#!/usr/bin/env python3

"""Repeatedly transmit an FT8 message through the Arduino/Si5351 interface."""

import argparse
import math
from pathlib import Path
import subprocess
import sys
import time

import serial


DEFAULT_MESSAGE = "CQ AC8SS EN82"
#DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_PORT = "/dev/ttyUSB0"
BAUD = 115200
HANDSHAKE_TIMEOUT = 15.0
SYMBOL_PERIOD = 0.160
SYMBOL_COUNT = 79
BASE_FREQ = 1_407_500_000
TONE_SPACING = 625
MSG2FREQ = Path(__file__).resolve().parent / "ft8_lib" / "msg2freq"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Continuously transmit a 20 m FT8 message until Ctrl-C"
    )
    parser.add_argument("--msg", default=DEFAULT_MESSAGE, help="FT8 message to send")
    parser.add_argument("--port", default=DEFAULT_PORT, help="Arduino serial port")
    parser.add_argument("--freq", type=int, default=BASE_FREQ,
                        help=f"base frequency in hundredths of Hz (default: {BASE_FREQ})")
    args = parser.parse_args()
    if not 0 <= args.freq <= (1 << 64) - 1 - 7 * TONE_SPACING:
        parser.error("--freq must fit in an unsigned 64-bit integer with room for all tones")
    return args


def load_frequencies(message, base_freq=BASE_FREQ, converter=MSG2FREQ):
    try:
        result = subprocess.run(
            [str(converter), "--freq", str(base_freq), message],
            check=True,
            capture_output=True,
            text=True,
        )
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


def wait_for_ft8_slot():
    """Wait for the next 15-second wall-clock boundary and return its epoch."""
    now = time.time()
    next_slot = math.floor(now / 15.0 + 1.0) * 15.0
    print(
        "Waiting for FT8 slot "
        + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(next_slot)),
        file=sys.stderr,
    )

    while True:
        remaining = next_slot - time.time()
        if remaining <= 0:
            return next_slot
        if remaining > 0.020:
            time.sleep(remaining - 0.010)


def transmit_frame(ser, frequencies):
    # Establish a known-safe state and preload tone zero before the boundary.
    send_command(ser, "OFF")
    send_command(ser, f"F {frequencies[0]}")
    slot = wait_for_ft8_slot()

    output_may_be_on = False
    try:
        start = time.monotonic()
        output_may_be_on = True
        send_command(ser, "ON")
        print(f"Transmission started at UTC epoch {slot:.3f}", file=sys.stderr)

        for index, frequency in enumerate(frequencies[1:], start=1):
            target = start + index * SYMBOL_PERIOD
            wait_until(target)
            lateness = time.monotonic() - target
            if lateness > 0.001:
                print(
                    f"Symbol {index:02d} dispatch late by {lateness * 1000:.3f} ms",
                    file=sys.stderr,
                )
            send_command(ser, f"F {frequency}")

        wait_until(start + SYMBOL_COUNT * SYMBOL_PERIOD)
    except BaseException:
        if output_may_be_on:
            try:
                send_command(ser, "OFF")
            except Exception as cleanup_error:
                print(f"Warning: failed to turn RF off: {cleanup_error}", file=sys.stderr)
        raise
    else:
        send_command(ser, "OFF")
        print("Transmission complete; RF output is off.", file=sys.stderr)


def main():
    args = parse_args()

    try:
        frequencies = load_frequencies(args.msg, args.freq)
        with serial.Serial(args.port, BAUD, timeout=1) as ser:
            handshake(ser, args.port)
            while True:
                transmit_frame(ser, frequencies)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (serial.SerialException, TimeoutError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
