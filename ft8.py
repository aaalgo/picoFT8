import re

from models import MessageType


CALL_RE = r"[A-Z0-9/]+"
GRID_RE = r"[A-R]{2}\d{2}"
REPORT_RE = r"[+-]\d{2}"
R_REPORT_RE = r"R[+-]\d{2}"


def classify_message(text: str) -> MessageType:
    text = text.strip().upper()
    parts = text.split()

    if not parts:
        return MessageType.UNKNOWN

    # CQ AC8SS EN82
    # CQ DX AC8SS EN82
    if parts[0] == "CQ":
        return MessageType.CQ

    # Standard directed messages normally have:
    # <receiver> <sender> <payload>
    if len(parts) != 3:
        return MessageType.UNKNOWN

    payload = parts[2]

    if re.fullmatch(GRID_RE, payload):
        return MessageType.GRID

    if re.fullmatch(REPORT_RE, payload):
        return MessageType.REPORT

    if re.fullmatch(R_REPORT_RE, payload):
        return MessageType.R_REPORT

    if payload == "RRR":
        return MessageType.RRR

    if payload == "RR73":
        return MessageType.RR73

    if payload == "73":
        return MessageType.MSG_73

    return MessageType.UNKNOWN
