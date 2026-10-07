"""Owned, best-effort UI bookmarks; never a source of lifecycle authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import has_control

MAX_RECORD_BYTES = 16 * 1024
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z", re.ASCII)
_SESSION = re.compile(r"\$[0-9]+\Z", re.ASCII)
_VIEWS = frozenset({"all", "local", "host", "open", "attached"})
_IDENTITY_FIELDS = ("hostId", "serverGeneration", "sessionId", "createdAt")


def session_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    host, generation, session, created = (value.get(key) for key in _IDENTITY_FIELDS)
    if (
        not isinstance(host, str)
        or _HOST.fullmatch(host) is None
        or not isinstance(generation, str)
        or not generation.startswith("tmux-v1:")
        or len(generation) > 4096
        or has_control(generation)
        or not isinstance(session, str)
        or _SESSION.fullmatch(session) is None
        or len(session) > 128
        or type(created) is not int
        or not 0 <= created <= 2**63 - 1
    ):
        return None
    return dict(zip(_IDENTITY_FIELDS, (host, generation, session, created), strict=True))


@dataclass(frozen=True, slots=True)
class ViewPreference:
    view: str = "all"
    host_id: str | None = None
    last_used: Mapping[str, object] | None = None


class ViewPreferenceStore:
    def __init__(self, endpoint: str, *, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        configured = Path(env.get("XDG_STATE_HOME", ""))
        base = configured if configured.is_absolute() else Path.home() / ".local/state"
        self.directory = base / "rofi-tmux-plus"
        self.endpoint = endpoint
        key = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
        self.path = self.directory / f"view-{key}.json"
        self.lock_path = self.directory / f"view-{key}.lock"

    def _directory(self, *, create: bool = False) -> bool:
        try:
            if create:
                self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            return (
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o700
            )
        except OSError:
            return False

    def load(self) -> ViewPreference:
        if not self._directory():
            return ViewPreference()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > MAX_RECORD_BYTES
                ):
                    return ViewPreference()
                raw = os.read(fd, MAX_RECORD_BYTES + 1)
            finally:
                os.close(fd)
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"schemaVersion", "endpointHostId", "view", "hostId", "lastUsed"}
                or type(value["schemaVersion"]) is not int
                or value["schemaVersion"] != 1
                or value["endpointHostId"] != self.endpoint
                or not isinstance(value["view"], str)
                or value["view"] not in _VIEWS
            ):
                return ViewPreference()
            host = value["hostId"]
            if value["view"] == "host":
                if not isinstance(host, str) or _HOST.fullmatch(host) is None:
                    return ViewPreference()
            elif host is not None:
                return ViewPreference()
            identity = session_identity(value["lastUsed"])
            if value["lastUsed"] is not None and (
                identity is None or set(value["lastUsed"]) != set(_IDENTITY_FIELDS)
            ):
                return ViewPreference()
            return ViewPreference(value["view"], host, identity)
        except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
            return ViewPreference()

    def update(self, view: str, host_id: str | None, *, opened: object = None) -> bool:
        """Merge an explicit UI action under a short, nonblocking lock."""
        if view not in _VIEWS or (
            view == "host" and (not isinstance(host_id, str) or _HOST.fullmatch(host_id) is None)
        ):
            return False
        identity = session_identity(opened) if opened is not None else None
        if opened is not None and identity is None:
            return False
        if not self._directory(create=True):
            return False
        descriptor: int | None = None
        temporary: str | None = None
        try:
            descriptor = os.open(
                self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return False
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = self.load()
            payload = {
                "schemaVersion": 1,
                "endpointHostId": self.endpoint,
                "view": view,
                "hostId": host_id if view == "host" else None,
                "lastUsed": identity if opened is not None else previous.last_used,
            }
            encoded = (
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
            ).encode()
            if len(encoded) > MAX_RECORD_BYTES:
                return False
            fd, temporary = tempfile.mkstemp(prefix=".view-", dir=self.directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            return True
        except (OSError, ValueError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
