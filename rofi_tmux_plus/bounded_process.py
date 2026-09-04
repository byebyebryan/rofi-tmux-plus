"""Bounded, reaped subprocess capture for hostile external process output."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

_CHUNK_SIZE = 16 * 1024
_POLL_SECONDS = 0.005


@dataclass(frozen=True, slots=True)
class BoundedCompleted:
    """A fully reaped process result whose captured streams never exceed their caps."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    overflow_streams: frozenset[str] = frozenset()


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> BoundedCompleted:
    """Run ``argv`` while bounded reader threads drain and always reap the child."""
    if timeout <= 0 or stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("bounded process limits must be positive")
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow_streams: set[str] = set()
    overflow_lock = threading.Lock()

    def read_stream(stream: object, buffer: bytearray, limit: int, label: str) -> None:
        reader = stream
        while True:
            remaining = limit - len(buffer)
            chunk = reader.read(min(_CHUNK_SIZE, max(1, remaining + 1)))
            if not chunk:
                return
            if len(chunk) <= remaining:
                buffer.extend(chunk)
                continue
            buffer.extend(chunk[:remaining])
            with overflow_lock:
                overflow_streams.add(label)
            # Continue draining after the main thread terminates the child.
            while reader.read(_CHUNK_SIZE):
                pass
            return

    readers = (
        threading.Thread(
            target=read_stream, args=(process.stdout, stdout, stdout_limit, "stdout"), daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(process.stderr, stderr, stderr_limit, "stderr"), daemon=True
        ),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    stopping = False

    def stop_group(sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            else:  # pragma: no cover - supported for the subprocess API boundary
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    try:
        while process.poll() is None:
            if overflow_streams or time.monotonic() >= deadline:
                timed_out = not overflow_streams
                stopping = True
                stop_group(signal.SIGTERM)
                break
            time.sleep(_POLL_SECONDS)
        if stopping and process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                stop_group(signal.SIGKILL)
        process.wait()
    finally:
        if process.poll() is None:
            stop_group(signal.SIGKILL)
            process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
    return BoundedCompleted(
        process.returncode,
        bytes(stdout).decode("utf-8", errors="replace"),
        bytes(stderr).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        overflow_streams=frozenset(overflow_streams),
    )
