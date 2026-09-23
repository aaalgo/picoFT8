import asyncio
import unittest

from simulator import NS, SLOT_NS, APIError, Offer, Simulator, Station, next_target, parse_args
from server import CALLSIGN2QSO, create_app
from qso import QSOStatus


class TimingAndStationTests(unittest.TestCase):
    def test_allowance_and_parity(self):
        self.assertEqual(next_target(13 * NS, 10 * NS), 30 * NS)
        self.assertEqual(next_target(13 * NS, 10 * NS, 1), 45 * NS)
        self.assertEqual(next_target(5 * NS, 10 * NS), 15 * NS)
        self.assertEqual(next_target(5 * NS + 1, 10 * NS), 30 * NS)
        for second in range(90):
            for tx_parity in (0, 1):
                target = next_target(second * NS, 10 * NS, tx_parity)
                self.assertGreaterEqual(target - second * NS, 10 * NS)
                self.assertEqual((target // SLOT_NS) % 2, tx_parity)

    def test_station_repeats_and_only_advances_on_relevant_opposite_parity(self):
        station = Station('W1ABC', 'EN82', -12)
        station.hear('CQ AC8SS EN82', 0)
        self.assertIsNone(station.transmit(0))
        self.assertEqual(station.transmit(15 * NS), 'AC8SS W1ABC EN82')
        self.assertEqual(station.transmit(45 * NS), 'AC8SS W1ABC EN82')
        station.hear('W1ABC AC8SS -08', 45 * NS)  # Cannot hear on TX parity.
        station.hear('OTHER AC8SS -08', 60 * NS)
        station.hear('W1ABC OTHER -08', 60 * NS)
        self.assertEqual(station.transmit(75 * NS), 'AC8SS W1ABC EN82')
        station.hear('W1ABC AC8SS -08', 90 * NS)
        station.hear('CQ AC8SS EN82', 120 * NS)  # Does not reset progress.
        self.assertEqual(station.transmit(135 * NS), 'AC8SS W1ABC R-12')
        station.hear('W1ABC AC8SS RR73', 150 * NS)
        self.assertEqual(station.transmit(165 * NS), 'AC8SS W1ABC 73')
        self.assertIsNone(station.transmit(195 * NS))
        station.hear('W1ABC AC8SS RR73', 210 * NS)
        self.assertEqual(station.transmit(225 * NS), 'AC8SS W1ABC 73')

    def test_lowercase_cli(self):
        args = parse_args(['--callsign', 'w1abc', '--grid', 'en82'])
        self.assertEqual((args.callsign, args.grid), ('W1ABC', 'EN82'))
        self.assertEqual(args.master, 'http://127.0.0.1:7777')


class SimulatorTests(unittest.IsolatedAsyncioTestCase):
    def simulator(self, api=None, drop_rate='0'):
        args = parse_args(['--callsign', 'W1ABC', '--grid', 'en82', '--seed', '42',
                           '--drop-rate', drop_rate, '--background', '0'])
        self.now = 0
        return Simulator(args, api=api, now=lambda: self.now)

    async def test_late_offer_discarded_and_worker_continues(self):
        class SlowAPI:
            def request(inner, path, body=None):
                self.now = int(path.split('=')[1]) + 1
                return {'message': 'CQ AC8SS EN82', 'handle': None}
        sim = self.simulator(SlowAPI())
        worker = asyncio.create_task(sim.network_worker())
        try:
            sim.enqueue('todo')
            with self.assertLogs('simulator', level='WARNING'):
                await asyncio.wait_for(sim.jobs.join(), 2)
            self.assertEqual(sim.offers, {})
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_coin_losses_do_not_cancel_tx_and_peer_repeats(self):
        sim = self.simulator(drop_rate='1')
        sim.tx_parity = 0
        cq = Offer('CQ AC8SS EN82', None)
        sim.finish_slot(0, cq, None)
        self.assertIsNone(sim.station.master)
        self.assertEqual(sim.jobs.get_nowait(), ('tx', (0, cq)))
        self.assertEqual(sim.jobs.get_nowait(), ('todo', None))
        sim.station.hear(cq.message, 0)
        _, message = sim.begin_slot(15 * NS)
        sim.finish_slot(15 * NS, None, message)
        self.assertEqual(sim.jobs.get_nowait(), ('rx', []))
        self.assertEqual(sim.begin_slot(45 * NS)[1], message)

    async def test_clock_keeps_running_during_http(self):
        import threading
        started, release = threading.Event(), threading.Event()

        class BlockingAPI:
            def request(inner, path, body=None):
                started.set()
                release.wait(2)
                return {'message': 'CQ AC8SS EN82', 'handle': None}
        sim = self.simulator(BlockingAPI())
        worker = asyncio.create_task(sim.network_worker())
        try:
            sim.enqueue('todo')
            await asyncio.to_thread(started.wait, 1)
            self.assertTrue(started.is_set())
            self.now = 15 * NS
            self.assertEqual(sim.begin_slot(self.now), (None, None))
            sim.finish_slot(self.now, None, None)
            release.set()
            with self.assertLogs('simulator', level='WARNING'):
                await asyncio.wait_for(sim.jobs.join(), 2)
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_end_to_end_with_real_server_and_preserved_prefetched_handles(self):
        await self.run_exchange('0')

    async def test_end_to_end_with_coin_flip_losses(self):
        await self.run_exchange('0.5')

    async def run_exchange(self, drop_rate):
        import tempfile
        from pathlib import Path
        CALLSIGN2QSO.clear()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(f'sqlite:///{Path(directory) / "test.sqlite3"}', now_ns=lambda: self.now)

            class FlaskAPI:
                def request(inner, path, body=None):
                    with app.test_client() as client:
                        response = client.get(path) if body is None else client.post(path, json=body)
                        if response.status_code >= 400:
                            raise APIError(f'HTTP {response.status_code}: {response.get_json()}')
                        return response.get_json()

            sim = self.simulator(FlaskAPI(), drop_rate)
            worker = asyncio.create_task(sim.network_worker())
            try:
                sim.enqueue('todo')
                await asyncio.wait_for(sim.jobs.join(), 2)
                with self.assertLogs('simulator', level='INFO') as logs:
                    for slot in range(1, 301):
                        self.now = slot * SLOT_NS
                        offer, message = sim.begin_slot(self.now)
                        self.now += 13 * NS
                        sim.finish_slot(slot * SLOT_NS, offer, message)
                        await asyncio.wait_for(sim.jobs.join(), 2)
                        if CALLSIGN2QSO.get('W1ABC') and CALLSIGN2QSO['W1ABC'].status == QSOStatus.COMPLETED:
                            break
                self.assertEqual(CALLSIGN2QSO['W1ABC'].status, QSOStatus.COMPLETED)
                self.assertFalse(any('failed' in log for log in logs.output), logs.output)
                records = FlaskAPI().request('/api/query/')
                self.assertTrue(records)
                for record in records:
                    utc_ns = record['calibrated_utc_ns']
                    self.assertEqual(utc_ns % SLOT_NS, 0)
                    self.assertNotEqual((utc_ns // SLOT_NS) % 2, sim.tx_parity)
                self.assertEqual(records[-1]['decode']['message'], 'AC8SS W1ABC 73')
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                CALLSIGN2QSO.clear()


if __name__ == '__main__':
    unittest.main()
