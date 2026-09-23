#!/usr/bin/env python3
"""Display the master's FT8 message history and poll for new rows every 5 seconds."""

import argparse
from datetime import datetime, timezone
import json
import re
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


POLL_INTERVAL = 5.0
AC8SS = re.compile(r"(?<![A-Z0-9])AC8SS(?![A-Z0-9])", re.IGNORECASE)


def clean_text(value):
    """Keep server-provided text on one line without terminal control codes."""
    return "".join(char if char.isprintable() else " " for char in str(value))


def format_row(record, color=True):
    message = clean_text(record["decode"]["message"])
    seconds, nanos = divmod(record["calibrated_utc_ns"], 1_000_000_000)
    utc = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    snr = record["decode"].get("snr_db", "-")
    row = (
        f'{record["message_id"]:8d}  {utc}.{nanos // 1_000_000:03d}  '
        f'{clean_text(record["direction"]):3}  '
        f'{clean_text(record.get("site_id", "-")):>6}  '
        f'{clean_text(snr):>6}  {message}'
    )
    if color and AC8SS.search(message):
        return f"\033[1;33m{row}\033[0m"
    return row


def monitor(master, show_all=False):
    base = master.rstrip("/")
    if "://" not in base:
        base = "http://" + base
    next_id = 0
    color = sys.stdout.isatty()
    print(f'{"ID":>8}  {"UTC":23}  DIR    SITE     SNR  MESSAGE', flush=True)
    while True:
        started = time.monotonic()
        try:
            with urlopen(
                f"{base}/api/query/?min_message_id={next_id}",
                timeout=POLL_INTERVAL,
            ) as response:
                records = json.load(response)
            if not isinstance(records, list):
                raise ValueError("expected a JSON array of messages")
            # Format the entire batch before printing or advancing the cursor.
            rows = []
            cursor = next_id
            for record in sorted(records, key=lambda item: item["message_id"]):
                if record["message_id"] < cursor:
                    continue
                if show_all or AC8SS.search(clean_text(record["decode"]["message"])):
                    rows.append(format_row(record, color))
                cursor = record["message_id"] + 1
            if rows:
                print("\n".join(rows), flush=True)
            next_id = cursor
        except (URLError, OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            print(f"monitor: {clean_text(exc)}; retrying from ID {next_id}",
                  file=sys.stderr, flush=True)
        time.sleep(max(0, POLL_INTERVAL - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", default="127.0.0.1:7777",
                        help="master host:port or URL (default: %(default)s)")
    parser.add_argument("--all", action="store_true",
                        help="show all messages (default: only messages mentioning AC8SS)")
    args = parser.parse_args()
    try:
        monitor(args.master, show_all=args.all)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
