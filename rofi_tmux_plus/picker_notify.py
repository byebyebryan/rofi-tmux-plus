"""Non-authoritative native wakeup metadata; it selects no command or action."""

from __future__ import annotations

import re

from .picker_runtime import read_private, write_private

NOTIFY_FILE = "notify"
NOTIFY_LIMIT = 192
WATCH_REQUIRED_ENV = "ROFI_TMUX_PLUS_WATCH_REQUIRED"
_PATTERN = re.compile(rb"TP1 ([0-9]{1,19}) ([0-9]{1,19}) ([0-9]{1,19}) ([0-9]{1,10}) ([01])\n")


def decode_notification(raw):
    match = _PATTERN.fullmatch(raw)
    if match is None:
        raise ValueError("malformed picker notification")
    sequence, received, expiry, pid, ready = (int(part) for part in match.groups())
    if max(sequence, received, expiry) > 2**63 - 1 or pid > 2**31 - 1 or ready and pid == 0:
        raise ValueError("picker notification exceeds numeric bounds")
    return {
        "sequence": sequence,
        "receivedAt": received,
        "expiresAt": expiry,
        "pid": pid,
        "ready": bool(ready),
    }


def read_notification(root):
    raw = read_private(root, NOTIFY_FILE, limit=NOTIFY_LIMIT)
    if raw is None:
        raise ValueError("picker watch notification is absent")
    return decode_notification(raw)


def write_notification(root, *, sequence, received, expiry=0, pid=0, ready=False):
    raw = f"TP1 {sequence} {received} {expiry} {pid} {int(ready)}\n".encode("ascii")
    decode_notification(raw)
    write_private(root, NOTIFY_FILE, raw, limit=NOTIFY_LIMIT)


def delivery_current(value, now):
    return value["ready"] and 0 <= now - value["receivedAt"] < 10000
