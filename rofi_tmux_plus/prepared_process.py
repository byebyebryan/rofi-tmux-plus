"""Select a trusted external Observer CLI for prepared Fleet v1 operations."""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import sys
import time
from functools import cached_property
from pathlib import Path

from .owned_process import EXEC_ENTRY
from .picker_runtime import clock_domain
from .wire import decode_document

CLIENT_ENV = "ROFI_TMUX_PLUS_OBSERVER_CLIENT"
FRAME_LIMIT = 1_048_576 + 16_384


class PreparedProcess:
    """The selected CLI validates Fleet v1 and authenticates its local IPC peer."""

    def __init__(self, environment):
        selected = Path(environment[CLIENT_ENV])
        if not selected.is_absolute():
            raise ValueError(f"{CLIENT_ENV} must name an absolute executable path")
        self.executable = selected.resolve(strict=True)
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise ValueError(f"{CLIENT_ENV} is not an executable file")
        self.environment = {
            key: value
            for key, value in environment.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }

    def command(self, *arguments):
        return [
            sys.executable,
            "-c",
            EXEC_ENTRY,
            str(os.getpid()),
            str(self.executable),
            *arguments,
        ]

    def _output(self, *arguments, limit):
        deadline = time.monotonic() + 1.0
        with subprocess.Popen(
            self.command(*arguments),
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as child:
            try:
                output = bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise ValueError("prepared Observer command timed out")
                        block = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(output)))
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > limit:
                            raise ValueError("prepared Observer response exceeded its bound")
                if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    raise ValueError("prepared Observer command failed")
                return bytes(output)
            except subprocess.TimeoutExpired as error:
                child.kill()
                raise ValueError("prepared Observer command timed out") from error
            except (ValueError, OSError):
                child.kill()
                raise

    @cached_property
    def context(self):
        value = self._output("context", limit=33)
        if re.fullmatch(rb"[0-9a-f]{32}\n", value) is None:
            raise ValueError("prepared Observer returned an invalid desktop context")
        return value[:-1].decode("ascii")

    def watch_command(self, context):
        return self.command("watch", "--json", "--context-id", context)

    def read_cached(
        self,
        context,
        *,
        operation="snapshot",
        expected_host=None,
        publisher_id=None,
        sources=None,
        ticket_id=None,
    ):
        if operation not in ("snapshot", "refresh", "refresh_status"):
            raise ValueError("unknown prepared Observer operation")
        arguments = [operation, "--json", "--context-id", context]
        if operation == "snapshot":
            arguments.extend(("--access", "cached"))
        if expected_host is not None:
            arguments.extend(("--expected-host", expected_host))
        if publisher_id is not None:
            arguments.extend(("--publisher-id", publisher_id))
        for source in sources or ():
            arguments.extend(("--source", source["hostId"] + ":" + source["source"]))
        if ticket_id is not None:
            arguments.extend(("--ticket-id", ticket_id))
        frame = decode_frame(self._output(*arguments, limit=FRAME_LIMIT))
        view = frame["snapshot"]
        expected_kind = "view" if operation == "snapshot" else "refresh_result"
        if (
            frame["kind"] != expected_kind
            or frame["contextId"] != context
            or frame["clock"] != clock_domain()
            or expected_host is not None
            and view["mesh"]["localHostId"] != expected_host
            or publisher_id is not None
            and frame["readerId"] != publisher_id
            or expected_kind == "refresh_result"
            and frame["ticket"] is None
        ):
            raise ValueError("prepared Observer returned a different operation or scope")
        return frame


def select_process(environment):
    return PreparedProcess(environment) if CLIENT_ENV in environment else None


def decode_frame(raw):
    # The external client validates every incoming IPC frame. Fence the process
    # interface here before handing its output to the picker or its watch.
    try:
        frame = decode_document(raw, limit=FRAME_LIMIT)
    except RecursionError as error:
        raise ValueError("prepared Observer response nesting exceeded its bound") from error
    if (
        not isinstance(frame, dict)
        or frame.get("protocol") != "tmux-observer.fleet.v1"
        or type(frame.get("schemaVersion")) is not int
        or frame["schemaVersion"] != 1
        or frame.get("kind")
        not in ("status", "view", "heartbeat", "gap", "resync", "error", "refresh_result")
        or not isinstance(frame.get("readerId"), str)
        or not isinstance(frame.get("contextId"), str)
        or type(frame.get("sequence")) is not int
        or frame["sequence"] < 0
        or not isinstance(frame.get("clock"), dict)
        or not isinstance(frame.get("snapshot"), dict)
    ):
        raise ValueError("prepared Observer returned an unsupported Fleet v1 frame")
    return frame
