"""One bounded public watch subscription, owned for one picker lifetime."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .observer_client import observer_api
from .owned_process import PARENT_GUARD
from .picker_notify import write_notification
from .prepared_model import boottime_ms, clock_domain

WATCH_ENTRY = (
    PARENT_GUARD
    + """
from tmux_observer_client.cli import main
raise SystemExit(main())
"""
)


def notification_material(frame, now):
    """Ignore quiet receipt renewals; keep rows, health, scope and ticket results."""
    view = frame["snapshot"]
    if view is None:
        raise ValueError("watch frame has no prepared view")
    owners = []
    expiries = []
    for host in view["hosts"]:
        owner = host["owner"]
        expiry = owner["localExpiry"]
        current = (
            view["mesh"]["state"] in {"ready", "local_only"}
            and owner["receipt"] is not None
            and owner["receipt"]["state"] == "ready"
            and expiry is not None
            and expiry > now
        )
        if current:
            expiries.append(expiry)
        owners.append(
            {
                "host": {key: host[key] for key in ("hostId", "display", "local", "sessions")},
                "owner": {
                    key: owner[key]
                    for key in (
                        "source",
                        "clock",
                        "publisherId",
                        "serverGeneration",
                        "transport",
                        "error",
                    )
                },
                "current": current,
            }
        )
    desktop = view["desktop"]
    desktop_current = (
        desktop["state"] == "ready"
        and desktop["expiresAt"] is not None
        and desktop["expiresAt"] > now
    )
    if desktop_current:
        expiries.append(desktop["expiresAt"])
    material = {
        "recovery": frame["kind"] if frame["kind"] in {"gap", "resync", "error"} else None,
        "reader": frame["readerId"],
        "context": frame["contextId"],
        "clock": frame["clock"],
        "mesh": view["mesh"],
        "owners": owners,
        "desktop": {key: desktop[key] for key in ("state", "epoch", "error")},
        "desktopCurrent": desktop_current,
        "ticket": frame["ticket"],
        "error": frame["error"],
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    return digest, min(expiries, default=0)


class OwnedWatch:
    """Reconnect only this read-only subscription; never activate a service."""

    def __init__(self, runtime, context, environment, *, command=None, api=None):
        self.runtime, self.context = runtime, context
        self.api = observer_api() if api is None else api
        self.environment = dict(environment)
        # A managed frozen-wheel root is not necessarily on child sys.path.
        self.environment["PYTHONPATH"] = str(Path(self.api.prepared.__file__).resolve().parents[1])
        self.command = command or [
            sys.executable,
            "-c",
            WATCH_ENTRY,
            str(os.getpid()),
            "watch",
            "--json",
            "--context-id",
            context,
        ]
        self.stop_event = threading.Event()
        self.thread = None
        self.child = None
        self.sequence = 0
        self.last_material = None

    def _publish(self, *, received, expiry=0, ready=False):
        write_notification(
            self.runtime,
            sequence=self.sequence,
            received=received,
            expiry=expiry,
            pid=self.child.pid if self.child is not None else 0,
            ready=ready,
        )

    def start(self):
        if self.thread is not None:
            raise ValueError("picker watch is already owned")
        self._publish(received=boottime_ms())
        self.thread = threading.Thread(target=self._run, name="tmux-plus-watch", daemon=False)
        self.thread.start()
        return self

    @staticmethod
    def _reap(child):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=0.7)
        else:
            child.wait()
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def _subscription(self):
        child = subprocess.Popen(
            self.command,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        self.child = child
        partial = bytearray()
        partial_started = None
        stderr_bytes = 0
        last_frame = boottime_ms()
        reader, sequence = None, -1
        clock = None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, label in ((child.stdout, "out"), (child.stderr, "err")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, label)
                while not self.stop_event.is_set():
                    now = boottime_ms()
                    if (
                        now - last_frame >= 10000
                        or partial_started is not None
                        and now - partial_started >= 2000
                    ):
                        raise ValueError("picker watch exceeded its stream deadline")
                    for key, _mask in selector.select(timeout=0.1):
                        raw = os.read(key.fileobj.fileno(), 65536)
                        if not raw:
                            selector.unregister(key.fileobj)
                            if key.data == "out":
                                return
                            continue
                        if key.data == "err":
                            stderr_bytes += len(raw)
                            if stderr_bytes > 65536:
                                raise ValueError("picker watch diagnostics exceeded their bound")
                            continue
                        if not partial:
                            partial_started = now
                        partial.extend(raw)
                        while b"\n" in partial:
                            line, _, remainder = partial.partition(b"\n")
                            partial = bytearray(remainder)
                            value = self.api.protocol.decode_document(
                                bytes(line) + b"\n", limit=self.api.protocol.FRAME_LIMIT
                            )
                            self.api.protocol.validate_fleet_frame(value)
                            if (
                                value["clock"] != clock_domain()
                                or value["contextId"] != self.context
                                or reader is not None
                                and (
                                    value["readerId"] != reader
                                    or value["clock"] != clock
                                    or value["sequence"] <= sequence
                                )
                            ):
                                raise ValueError("picker watch scope or ordering changed")
                            reader, sequence, clock = (
                                value["readerId"],
                                value["sequence"],
                                value["clock"],
                            )
                            now = boottime_ms()
                            if partial_started is not None and now - partial_started >= 2000:
                                raise ValueError("picker watch frame arrived after its deadline")
                            last_frame = now
                            material, expiry = notification_material(value, now)
                            if self.last_material != material:
                                self.sequence += 1
                                self.last_material = material
                            self._publish(received=now, expiry=expiry, ready=True)
                        if len(partial) >= self.api.protocol.FRAME_LIMIT:
                            raise ValueError("picker watch frame exceeded its bound")
                        if not partial:
                            partial_started = None
        finally:
            self._reap(child)
            self.child = None

    def _run(self):
        retry_ms = 250
        while not self.stop_event.is_set():
            try:
                self._subscription()
            except (OSError, ValueError, TypeError, KeyError):
                pass
            self.last_material = None
            self.sequence += 1
            try:
                self._publish(received=boottime_ms())
            except (OSError, ValueError):
                # Loss of this owned root ends the helper; native checks expose it.
                return
            if self.stop_event.wait(retry_ms / 1000):
                return
            retry_ms = min(4000, retry_ms * 2)

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise RuntimeError("owned picker watch did not stop within its deadline")

    def __enter__(self):
        return self.start()

    def __exit__(self, *_error):
        self.stop()
