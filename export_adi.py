#!/usr/bin/env python3
"""Export correspondence with one callsign as an ADIF contact on stdout."""

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from urllib.error import URLError
from urllib.request import urlopen


MASTER_QUERY = "http://127.0.0.1:7777/api/query/"
LOCAL_CALL = "AC8SS"


def export_adi(records, callsign):
    """Combine stored exchanges into one contact; omit unknown fields."""
    exchanges = []
    grids = []
    for record in records:
        parts = record["decode"]["message"].upper().split()
        parts = [part.strip("<>") for part in parts]
        if len(parts) < 3:
            continue
        if parts[:2] in ([LOCAL_CALL, callsign], [callsign, LOCAL_CALL]):
            exchanges.append(record)
        # A grid must belong to the sender, including directed CQ messages.
        if parts[-2] == callsign and parts[-1] != "RR73" and re.fullmatch(
            r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", parts[-1]
        ):
            grids.append((record["calibrated_utc_ns"], parts[-1]))
    if not exchanges:
        return ""

    timestamps = [record["calibrated_utc_ns"] for record in exchanges]
    start = datetime.fromtimestamp(min(timestamps) // 1_000_000_000, timezone.utc)
    end = datetime.fromtimestamp(max(timestamps) // 1_000_000_000, timezone.utc)
    fields = [
        ("CALL", callsign),
        ("QSO_DATE", start.strftime("%Y%m%d")),
        ("TIME_ON", start.strftime("%H%M%S")),
        ("QSO_DATE_OFF", end.strftime("%Y%m%d")),
        ("TIME_OFF", end.strftime("%H%M%S")),
        ("BAND", "20m"),
        ("MODE", "FT8"),
    ]
    if grids:
        fields.append(("GRIDSQUARE", max(grids)[1]))
    return "<ADIF_VER:5>3.1.4\n<EOH>\n\n" + "".join(
        f"<{name}:{len(value)}>{value}\n" for name, value in fields
    ) + "<EOR>\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("callsign", type=str.upper)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", args.callsign):
        parser.error("callsign must contain letters, digits, or slash-separated components")
    try:
        with urlopen(MASTER_QUERY, timeout=10) as response:
            records = json.load(response)
        if not isinstance(records, list):
            raise ValueError("expected a JSON array of messages")
        output = export_adi(records, args.callsign)
    except (URLError, OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        print(f"export_adi: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
