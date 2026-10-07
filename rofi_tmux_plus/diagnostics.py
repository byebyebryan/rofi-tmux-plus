"""Opt-in bounded timing events, separate from public and Rofi output."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

TIMING_ENV = "TMUX_PLUS_TIMING_FILE"
MAX_TIMING_BYTES = 2 * 1024 * 1024


def record(name: str, started: float, **fields: object) -> None:
    configured = os.environ.get(TIMING_ENV)
    if not configured:
        return
    descriptor = None
    try:
        path = Path(configured)
        info = path.parent.lstat()
        if not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return
        descriptor = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_TIMING_BYTES
        ):
            return
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        event = {
            "event": name,
            "observedAt": time.time_ns() // 1_000_000,
            "durationMs": round((time.monotonic() - started) * 1000, 3),
            **fields,
        }
        encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        if os.fstat(descriptor).st_size + len(encoded) <= MAX_TIMING_BYTES:
            os.write(descriptor, encoded)
    except (OSError, ValueError, TypeError):
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def span(name: str, **fields: object):
    if not os.environ.get(TIMING_ENV):
        yield fields
        return
    started = time.monotonic()
    try:
        yield fields
    finally:
        record(name, started, **fields)


def timed(name: str, *, host_argument: int | None = None, process_result: bool = False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            fields = {}
            if host_argument is not None:
                host = args[host_argument] if len(args) > host_argument else kwargs.get("host")
                if host is not None:
                    fields["hostId"] = host.host_id
            with span(name, **fields) as event:
                result = function(*args, **kwargs)
                if process_result and os.environ.get(TIMING_ENV):
                    for stream in ("stdout", "stderr"):
                        raw = getattr(result, f"{stream}_bytes", None)
                        if not isinstance(raw, bytes):
                            raw = getattr(result, stream, "") or ""
                            if isinstance(raw, str):
                                raw = raw.encode("utf-8", errors="replace")
                        event[f"{stream}Bytes"] = len(raw)
                    event["returncode"] = getattr(result, "returncode", None)
                return result

        return wrapped

    return decorate
