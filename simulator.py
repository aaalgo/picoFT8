#!/usr/bin/env python3
"""Simulate a half-duplex FT8 master radio and one lossy remote station.

No radio hardware or third-party Python packages are required. API timestamps
are Unix nanoseconds. HTTP operations are ordered but never block the slot clock.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import math
import random
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

NS = 1_000_000_000
SLOT_NS = 15 * NS
MIN_TODO_ALLOWANCE = 10
LOG = logging.getLogger('simulator')


def parity(slot_ns: int) -> int:
    return (slot_ns // SLOT_NS) % 2


def next_target(now_ns: int, allowance_ns: int, tx_parity: int | None = None) -> int:
    target = (now_ns // SLOT_NS + 1) * SLOT_NS
    while target - now_ns < allowance_ns:
        target += SLOT_NS
    if tx_parity is not None and parity(target) != tx_parity:
        target += SLOT_NS
    return target


@dataclass(frozen=True)
class Offer:
    message: str
    handle: str | None


@dataclass
class Station:
    callsign: str
    grid: str
    report: int
    master: str | None = None
    tx_parity: int | None = None
    phase: int = 0
    payload: str | None = None

    def hear(self, message: str, slot_ns: int) -> None:
        parts = message.upper().split()
        if len(parts) != 3:
            return
        receiver, sender, payload = parts
        if sender == self.callsign or (self.master and sender != self.master):
            return
        if self.tx_parity is not None and parity(slot_ns) == self.tx_parity:
            return  # This station cannot listen while transmitting.
        if receiver == 'CQ':
            if self.phase == 0:
                self.master = sender
                self.tx_parity = 1 - parity(slot_ns)
                self.phase, self.payload = 1, self.grid
            return
        if receiver != self.callsign or self.master is None:
            return
        if re.fullmatch(r'[+-]\d{2}', payload) and self.phase <= 2:
            self.phase, self.payload = 2, f'R{self.report:+03d}'
        elif payload in ('RRR', 'RR73'):
            self.phase, self.payload = 3, '73'
        elif payload == '73':
            self.phase, self.payload = 3, None

    def transmit(self, slot_ns: int) -> str | None:
        if self.payload is None or parity(slot_ns) != self.tx_parity:
            return None
        message = f'{self.master} {self.callsign} {self.payload}'
        if self.payload == '73':
            # A repeated RR73 re-arms 73 if the first one was lost.
            self.payload = None
        return message


class APIError(Exception):
    pass


class API:
    def __init__(self, master: str, timeout: float):
        self.base = master.rstrip('/')
        self.timeout = timeout

    def request(self, path: str, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = Request(self.base + path, data=data,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(1024).decode('utf-8', errors='replace')
            raise APIError(f'HTTP {exc.code}: {detail}') from exc


class Simulator:
    def __init__(self, args, api=None, now=time.time_ns):
        self.now = now
        self.api = api or API(args.master, args.timeout)
        self.rng = random.Random(args.seed)
        self.station = Station(args.callsign, args.grid, self.rng.randint(-20, -1))
        self.allowance_ns = round(args.allowance * NS)
        self.drop_rate = args.drop_rate
        self.background = args.background
        self.site_id = args.site_id
        first = next_target(self.now(), self.allowance_ns)
        self.tx_parity = parity(first)
        self.offers: dict[int, Offer] = {}
        self.jobs = asyncio.Queue(maxsize=8)
        self.last_target = -1

    def enqueue(self, kind, value=None):
        try:
            self.jobs.put_nowait((kind, value))
        except asyncio.QueueFull:
            LOG.warning('HTTP backlog full: skipping %s; slot clock continues', kind)

    async def network_worker(self):
        while True:
            kind, value = await self.jobs.get()
            try:
                if kind == 'todo':
                    # Compute the target at dispatch, not while queued behind HTTP.
                    target = next_target(self.now(), self.allowance_ns, self.tx_parity)
                    if target <= self.last_target:
                        continue
                    self.last_target = target
                    result = await asyncio.to_thread(
                        self.api.request, f'/api/tx/todo/?utc_ns={target}')
                    if self.now() >= target:
                        LOG.warning('Discarding late offer for utc_ns=%d', target)
                        continue
                    if (not isinstance(result, dict) or
                            not isinstance(result.get('message'), str) or
                            not result['message'].strip() or 'handle' not in result or
                            (result['handle'] is not None and
                             not isinstance(result['handle'], str))):
                        raise APIError(f'Invalid offer: {result!r}')
                    self.offers[target] = Offer(result['message'], result['handle'])
                    LOG.info('Prepared utc_ns=%d: %s', target, result['message'])
                elif kind == 'tx':
                    slot_ns, offer = value
                    result = await asyncio.to_thread(self.api.request, '/api/tx/',
                                                    {'utc_ns': slot_ns, 'handle': offer.handle})
                    if result != {'recorded': True}:
                        raise APIError(f'Invalid TX acknowledgment: {result!r}')
                    LOG.info('Recorded TX utc_ns=%d', slot_ns)
                elif kind == 'rx':
                    result = await asyncio.to_thread(self.api.request, '/api/rx/', value)
                    if result != {'inserted': len(value)}:
                        raise APIError(f'Invalid RX acknowledgment: {result!r}')
                    LOG.info('Recorded %d RX decodes', len(value))
            except (APIError, OSError, ValueError) as exc:
                # No automatic retries; RX batches are not idempotent. TX could
                # be retried with the same handle/slot within its retention window.
                LOG.warning('%s failed (no automatic retry): %s', kind, exc)
            finally:
                self.jobs.task_done()

    def record(self, slot_ns, message, snr):
        return {'site_id': self.site_id, 'calibrated_utc_ns': slot_ns,
                'decode': {'message': message, 'snr_db': snr}}

    def background_records(self, slot_ns):
        records = []
        for _ in range(self.rng.randint(0, self.background)):
            # Random CQs are stored but never create additional master QSOs.
            call = 'W' + str(self.rng.randrange(10)) + ''.join(
                self.rng.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=3))
            if call in (self.station.callsign, self.station.master):
                continue
            grid = ''.join(self.rng.choices('ABCDEFGHIJKLMNOPQR', k=2))
            grid += f'{self.rng.randrange(100):02d}'
            records.append(self.record(slot_ns, f'CQ {call} {grid}', self.rng.randint(-24, 5)))
        return records

    def begin_slot(self, slot_ns):
        for old in list(self.offers):
            if old < slot_ns:
                del self.offers[old]
        offer = self.offers.pop(slot_ns, None)
        if parity(slot_ns) == self.tx_parity:
            return offer, None
        return None, self.station.transmit(slot_ns)

    def finish_slot(self, slot_ns, offer, peer_message):
        """Called only after the slot's +13s decode/completion deadline."""
        if offer is not None:
            self.enqueue('tx', (slot_ns, offer))
            if self.rng.random() >= self.drop_rate:
                self.station.hear(offer.message, slot_ns)
                LOG.info('Peer heard: %s', offer.message)
            else:
                LOG.info('Dropped master -> peer: %s', offer.message)
        if parity(slot_ns) != self.tx_parity:
            records = self.background_records(slot_ns)
            if peer_message is not None:
                if self.rng.random() >= self.drop_rate:
                    records.append(self.record(slot_ns, peer_message, self.rng.randint(-20, -1)))
                else:
                    LOG.info('Dropped peer -> master: %s', peer_message)
            self.enqueue('rx', records)
        else:
            # Ordered after TX acknowledgment, so consumed handles are not reused.
            # Intervening RX preserves issued offers and their original stages.
            self.enqueue('todo')

    async def wait_until(self, deadline_ns):
        while (remaining := deadline_ns - self.now()) > 0:
            await asyncio.sleep(min(remaining / NS, 0.25))

    async def run(self):
        LOG.info('Master TX parity=%s; allowance=%.1fs; loss=%.0f%% each direction',
                 'odd' if self.tx_parity else 'even', self.allowance_ns / NS,
                 self.drop_rate * 100)
        worker = asyncio.create_task(self.network_worker())
        self.enqueue('todo')
        slot_ns = (self.now() // SLOT_NS + 1) * SLOT_NS
        try:
            while True:
                await self.wait_until(slot_ns)
                if self.now() >= slot_ns + NS // 2:
                    LOG.warning('Missed slot utc_ns=%d; resynchronizing', slot_ns)
                    self.offers.clear()
                    self.enqueue('todo')
                    slot_ns = (self.now() // SLOT_NS + 1) * SLOT_NS
                    continue
                offer, peer_message = self.begin_slot(slot_ns)
                await self.wait_until(slot_ns + NS // 2)
                if self.now() >= slot_ns + NS:
                    LOG.warning('Missed RF start utc_ns=%d; skipping transmission', slot_ns)
                    offer, peer_message = None, None
                if offer:
                    LOG.info('Master TX utc_ns=%d: %s', slot_ns, offer.message)
                if peer_message:
                    LOG.info('Peer TX utc_ns=%d: %s', slot_ns, peer_message)
                await self.wait_until(slot_ns + 13 * NS)
                self.finish_slot(slot_ns, offer, peer_message)
                slot_ns += SLOT_NS
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', default='127.0.0.1:7777', help='server host:port or HTTP URL')
    parser.add_argument('--callsign', required=True, type=str.upper)
    parser.add_argument('--grid', required=True, type=str.upper)
    parser.add_argument('--seed', type=int, help='reproducible random traffic and losses')
    parser.add_argument('--drop-rate', type=float, default=0.5, help='loss probability in each direction (default: 0.5)')
    parser.add_argument('--allowance', type=float, default=MIN_TODO_ALLOWANCE, help='minimum advance request time in seconds (default: 10)')
    parser.add_argument('--timeout', type=float, default=10, help='HTTP socket timeout in seconds')
    parser.add_argument('--background', type=int, default=5, help='maximum random CQs per receive slot')
    parser.add_argument('--site-id', type=int, default=1)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Z0-9/]+', args.callsign):
        parser.error('--callsign must contain only letters, digits, or /')
    if not re.fullmatch(r'[A-R]{2}\d{2}', args.grid):
        parser.error('--grid must be a four-character Maidenhead grid, e.g. EN82')
    if not 0 <= args.drop_rate <= 1:
        parser.error('--drop-rate must be between 0 and 1')
    if any(not math.isfinite(v) or v <= 0 for v in (args.allowance, args.timeout)):
        parser.error('--allowance and --timeout must be positive finite seconds')
    if args.background < 0:
        parser.error('--background must be nonnegative')
    if '://' not in args.master:
        args.master = 'http://' + args.master
    parsed = urlsplit(args.master)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
        parser.error('--master must be a host:port or HTTP(S) base URL')
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.Formatter.converter = time.gmtime
    try:
        asyncio.run(Simulator(args).run())
    except KeyboardInterrupt:
        LOG.info('Stopped')


if __name__ == '__main__':
    main()
