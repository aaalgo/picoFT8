#!/usr/bin/env python3

"""Transmit scheduled server offers or a fixed FT8 message over Arduino/Si5351."""

import argparse
from dataclasses import dataclass
import json
import math
import queue
import threading
from pathlib import Path
import subprocess
import sys
import time

from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_MESSAGE = "CQ AC8SS EN82"
#DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_PORT = "/dev/ttyUSB0"
BAUD = 115200
HANDSHAKE_TIMEOUT = 15.0
SYMBOL_PERIOD = 0.160
SYMBOL_COUNT = 79
BASE_FREQ = 1_407_500_000
TONE_SPACING = 625
NS = 1_000_000_000
SLOT_NS = 15 * NS
MIN_TODO_ALLOWANCE = 10.0
TX_OFFSET_NS = NS // 2
MAX_START_LATENESS_NS = 50_000_000
ACK_RETENTION_NS = 600 * NS
MSG2FREQ = Path(__file__).resolve().parent / "ft8_lib" / "msg2freq"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--master", help="API server host:port or HTTP(S) URL")
    source.add_argument("--msg", help=f"fixed message (default: {DEFAULT_MESSAGE})")
    parser.add_argument("--port", default=DEFAULT_PORT, help="Arduino serial port")
    parser.add_argument("--freq", type=int, default=BASE_FREQ,
                        help=f"base frequency in hundredths of Hz (default: {BASE_FREQ})")
    parser.add_argument("--allowance", type=float, default=MIN_TODO_ALLOWANCE,
                        help="minimum server request lead time in seconds (default: 10)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="HTTP socket and message-conversion timeout in seconds")
    args = parser.parse_args(argv)
    if not 0 <= args.freq <= (1 << 64) - 1 - 7 * TONE_SPACING:
        parser.error("--freq must fit in an unsigned 64-bit integer with room for all tones")
    if any(not math.isfinite(value) or value <= 0 for value in (args.allowance, args.timeout)):
        parser.error("--allowance and --timeout must be positive finite seconds")
    if args.master:
        if "://" not in args.master:
            args.master = "http://" + args.master
        parsed = urlsplit(args.master)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            parser.error("--master must be a host:port or HTTP(S) base URL")
        args.master = args.master.rstrip("/")
    args.msg = args.msg or DEFAULT_MESSAGE
    return args


def load_frequencies(message, base_freq=BASE_FREQ, converter=MSG2FREQ, timeout=10.0):
    try:
        result = subprocess.run(
            [str(converter), "--freq", str(base_freq), message],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"msg2freq timed out after {timeout:g} seconds") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Converter not found: {converter}; build it with 'make -C ft8_lib'"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or f"exit status {exc.returncode}"
        raise RuntimeError(f"msg2freq failed: {detail}") from exc

    lines = result.stdout.splitlines()
    if len(lines) != SYMBOL_COUNT:
        raise RuntimeError(
            f"msg2freq returned {len(lines)} frequencies; expected {SYMBOL_COUNT}"
        )
    if any(not line.isdecimal() for line in lines):
        raise RuntimeError("msg2freq returned a non-decimal frequency")

    try:
        frequencies = [int(line) for line in lines]
    except ValueError as exc:
        raise RuntimeError("msg2freq returned a non-integer frequency") from exc

    valid_frequencies = {base_freq + tone * TONE_SPACING for tone in range(8)}
    invalid = [frequency for frequency in frequencies if frequency not in valid_frequencies]
    if invalid:
        raise RuntimeError(f"msg2freq returned invalid frequency: {invalid[0]}")

    return frequencies


def send_command(ser, command):
    ser.write((command + "\n").encode("ascii"))
    ser.flush()
    response = ser.readline().decode("ascii", errors="replace").strip()

    if not response:
        raise TimeoutError(f"Timed out waiting for a response to {command!r}")
    if response != "OK":
        raise RuntimeError(f"Command {command!r} failed: {response}")

    return response


def handshake(ser, port):
    print(f"Connecting to Arduino on {port}...", file=sys.stderr)
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT

    while time.monotonic() < deadline:
        ser.write(b"PING\n")
        ser.flush()
        response = ser.readline().decode("ascii", errors="replace").strip()
        if response == "OK":
            return
        if response:
            print(f"Arduino: {response}", file=sys.stderr)

    raise TimeoutError(
        f"Arduino on {port} did not respond to PING within "
        f"{HANDSHAKE_TIMEOUT:g} seconds"
    )


def wait_until(deadline):
    """Wait for an absolute deadline from the selected clock."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if remaining > 0.005:
            time.sleep(remaining - 0.002)


def next_target(now_ns, allowance_ns):
    target = (now_ns // SLOT_NS + 1) * SLOT_NS
    while target - now_ns < allowance_ns:
        target += SLOT_NS
    return target


def wait_for_utc(deadline_ns, stop=None):
    """Recheck UTC frequently so wall-clock adjustments do not shift a slot."""
    while True:
        remaining = (deadline_ns - time.time_ns()) / NS
        if remaining <= 0:
            return True
        delay = min(remaining, 0.1)
        if stop is not None:
            if stop.wait(delay):
                return False
        else:
            time.sleep(delay)


def transmit_frame(ser, frequencies, slot_ns=None):
    """Return the completed slot, or None if preparation/start missed its deadline."""
    if slot_ns is not None and time.time_ns() >= slot_ns:
        print(f"Skipping late frame utc_ns={slot_ns}", file=sys.stderr)
        return None
    # Only this thread ever touches the serial port.
    send_command(ser, "OFF")
    send_command(ser, f"F {frequencies[0]}")
    if slot_ns is None:
        slot_ns = next_target(time.time_ns(), NS)
    if time.time_ns() >= slot_ns:
        print(f"Serial preparation missed utc_ns={slot_ns}; skipping", file=sys.stderr)
        return None
    wait_for_utc(slot_ns + TX_OFFSET_NS)
    if time.time_ns() - (slot_ns + TX_OFFSET_NS) > MAX_START_LATENESS_NS:
        print(f"RF start missed utc_ns={slot_ns}; skipping", file=sys.stderr)
        return None

    output_may_be_on = False
    try:
        start = time.monotonic()
        output_may_be_on = True
        send_command(ser, "ON")
        print(f"Transmission started utc_ns={slot_ns}", file=sys.stderr)
        for index, frequency in enumerate(frequencies[1:], start=1):
            target = start + index * SYMBOL_PERIOD
            wait_until(target)
            lateness = time.monotonic() - target
            if lateness >= SYMBOL_PERIOD:
                raise RuntimeError(f"Missed symbol {index}; aborting frame")
            if lateness > 0.001:
                print(f"Symbol {index:02d} dispatch late by {lateness * 1000:.3f} ms",
                      file=sys.stderr)
            send_command(ser, f"F {frequency}")
        end = start + SYMBOL_COUNT * SYMBOL_PERIOD
        wait_until(end)
        if time.monotonic() - end >= SYMBOL_PERIOD:
            raise RuntimeError("Missed frame end; aborting frame")
        send_command(ser, "OFF")
        output_may_be_on = False
    finally:
        if output_may_be_on:
            try:
                send_command(ser, "OFF")
            except Exception as cleanup_error:
                print(f"Warning: failed to turn RF off: {cleanup_error}", file=sys.stderr)
    print("Transmission complete; RF output is off.", file=sys.stderr)
    return slot_ns


@dataclass(frozen=True)
class PreparedFrame:
    slot_ns: int
    message: str
    handle: str | None
    frequencies: tuple[int, ...]


class MasterAPI:
    def __init__(self, base, timeout):
        self.base, self.timeout = base, timeout

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base + path, data=data,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)


class ServerTransmitter:
    """Request/encode and acknowledge on workers; send on the caller's thread."""

    def __init__(self, args, api=None):
        self.args = args
        self.api = api or MasterAPI(args.master, args.timeout)
        self.stop = threading.Event()
        self.frames = queue.Queue(maxsize=2)
        self.acks = queue.Queue(maxsize=32)
        self.allowance_ns = round(args.allowance * NS)

    def prepare(self, slot_ns):
        result = self.api.request(f"/api/tx/todo/?utc_ns={slot_ns}")
        if (not isinstance(result, dict) or not isinstance(result.get("message"), str)
                or not result["message"].strip() or "handle" not in result
                or (result["handle"] is not None and not isinstance(result["handle"], str))):
            raise RuntimeError(f"Invalid server offer: {result!r}")
        if time.time_ns() >= slot_ns:
            return None
        frequencies = load_frequencies(result["message"], self.args.freq, timeout=self.args.timeout)
        if time.time_ns() >= slot_ns:
            return None
        return PreparedFrame(slot_ns, result["message"], result["handle"], tuple(frequencies))

    def request_loop(self):
        last_target = -1
        while not self.stop.is_set():
            target = next_target(time.time_ns(), self.allowance_ns)
            if target <= last_target:
                if not wait_for_utc(last_target, self.stop):
                    return
                continue
            last_target = target
            try:
                frame = self.prepare(target)
                if frame is None:
                    print(f"Discarding late preparation utc_ns={target}", file=sys.stderr)
                elif not self.stop.is_set():
                    self.frames.put_nowait(frame)
                    print(f"Prepared utc_ns={target}: {frame.message}", file=sys.stderr)
            except queue.Full:
                print(f"Preparation queue full; skipping utc_ns={target}", file=sys.stderr)
            except (OSError, URLError, ValueError, RuntimeError) as exc:
                print(f"Request/encoding failed for utc_ns={target}: {exc}", file=sys.stderr)
            # Do not eagerly request an unbounded series of future slots.
            if not wait_for_utc(target, self.stop):
                return

    def acknowledge(self, frame):
        body = {"utc_ns": frame.slot_ns, "handle": frame.handle}
        delay = 1.0
        while not self.stop.is_set():
            if time.time_ns() >= frame.slot_ns + ACK_RETENTION_NS:
                print(f"Acknowledgment expired utc_ns={frame.slot_ns}", file=sys.stderr)
                return
            try:
                result = self.api.request("/api/tx/", body)
                if result != {"recorded": True}:
                    raise ValueError(f"Invalid acknowledgment: {result!r}")
                print(f"Recorded TX utc_ns={frame.slot_ns}", file=sys.stderr)
                return
            except HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    print(f"Acknowledgment rejected utc_ns={frame.slot_ns}: {exc}", file=sys.stderr)
                    return
                print(f"Acknowledgment failed; retrying: {exc}", file=sys.stderr)
            except (OSError, URLError, ValueError) as exc:
                print(f"Acknowledgment failed; retrying: {exc}", file=sys.stderr)
            if self.stop.wait(delay):
                return
            delay = min(30.0, delay * 2)

    def acknowledgment_loop(self):
        while not self.stop.is_set():
            try:
                frame = self.acks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.acknowledge(frame)
            finally:
                self.acks.task_done()

    def send_prepared(self, ser, frame):
        completed = transmit_frame(ser, frame.frequencies, frame.slot_ns)
        if completed is not None:
            try:
                self.acks.put_nowait(frame)
            except queue.Full:
                print(f"Acknowledgment queue full; unrecorded TX utc_ns={frame.slot_ns}",
                      file=sys.stderr)

    def run(self, ser):
        workers = [threading.Thread(target=self.request_loop, name="ft8-request", daemon=True),
                   threading.Thread(target=self.acknowledgment_loop, name="ft8-ack", daemon=True)]
        print(f"Master TX every 15-second slot; "
              f"allowance={self.args.allowance:g}s", file=sys.stderr)
        for worker in workers:
            worker.start()
        try:
            while True:
                try:
                    frame = self.frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self.send_prepared(ser, frame)
                finally:
                    self.frames.task_done()
        finally:
            self.stop.set()
            for worker in workers:
                worker.join(timeout=0.2)


def main():
    args = parse_args()
    try:
        import serial
    except ImportError:
        print("Error: install pyserial to use the Arduino serial interface.", file=sys.stderr)
        return 1
    try:
        frequencies = None if args.master else load_frequencies(args.msg, args.freq, timeout=args.timeout)
        with serial.Serial(args.port, BAUD, timeout=1, write_timeout=1) as ser:
            handshake(ser, args.port)
            send_command(ser, "OFF")
            if args.master:
                ServerTransmitter(args).run(ser)
            else:
                while True:
                    transmit_frame(ser, frequencies)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (serial.SerialException, OSError, TimeoutError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
