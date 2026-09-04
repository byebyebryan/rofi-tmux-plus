"""Private, validated retained remote-inventory state for the future picker."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ContractError, clean_message
from .mesh_adapter import MeshSnapshot

_SCHEMA_VERSION = 1
_MAX_BYTES = 2 * 1024 * 1024
_MAX_HOSTS = 128
_MAX_SESSIONS = 256
_MAX_TEXT = 16 * 1024
_HOST_REQUIRED = {
    "hostId",
    "display",
    "local",
    "status",
    "observedAt",
    "nativeHostname",
    "serverGeneration",
    "route",
    "sessions",
}
_HOST_OPTIONAL = {"error", "lastSeenAt", "stale", "unavailable"}
_SESSION_REQUIRED = {
    "hostId",
    "serverGeneration",
    "sessionId",
    "createdAt",
    "name",
    "activityAt",
    "lastAttachedAt",
    "attachedClients",
    "pending",
    "windowCount",
    "sessionPath",
    "currentWindow",
    "currentPath",
}


def cache_directory(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "rofi-tmux-plus"


def _clean_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _text(value: object, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        isinstance(value, str) and len(value.encode("utf-8", errors="replace")) <= _MAX_TEXT
    )


def _number(value: object, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1
    )


def _session(value: object, host_id: str) -> bool:
    # The picker model requests neither panes nor ad-hoc option payloads.
    # Rejecting them keeps retained state strictly scoped to its own schema.
    if not isinstance(value, dict) or set(value) != _SESSION_REQUIRED:
        return False
    return value.get("hostId") == host_id and all(
        (
            _text(value.get("hostId")),
            _text(value.get("serverGeneration")),
            _text(value.get("sessionId")),
            _number(value.get("createdAt")),
            _text(value.get("name"), nullable=True),
            _number(value.get("activityAt"), nullable=True),
            _number(value.get("lastAttachedAt"), nullable=True),
            _number(value.get("attachedClients"), nullable=True),
            isinstance(value.get("pending"), bool),
            _number(value.get("windowCount"), nullable=True),
            _text(value.get("sessionPath"), nullable=True),
            _text(value.get("currentWindow"), nullable=True),
            _text(value.get("currentPath"), nullable=True),
        )
    )


def _host(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    keys = set(value)
    if not _HOST_REQUIRED <= keys or not keys <= (_HOST_REQUIRED | _HOST_OPTIONAL):
        return None
    host_id = value.get("hostId")
    sessions = value.get("sessions")
    if (
        not _text(host_id)
        or not _text(value.get("display"))
        or value.get("local") is not False
        or value.get("status") not in {"ok", "unreachable", "tmux_missing", "error"}
        or not _number(value.get("observedAt"))
        or not _text(value.get("nativeHostname"), nullable=True)
        or not _text(value.get("serverGeneration"), nullable=True)
        or not _text(value.get("route"), nullable=True)
        or not isinstance(sessions, list)
        or len(sessions) > _MAX_SESSIONS
        or any(not _session(session, host_id) for session in sessions)
    ):
        return None
    error = value.get("error")
    if error is not None and (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not _text(error.get("code"))
        or not _text(error.get("message"))
    ):
        return None
    if "lastSeenAt" in value and not _number(value["lastSeenAt"]):
        return None
    if "stale" in value and not isinstance(value["stale"], bool):
        return None
    if "unavailable" in value and not isinstance(value["unavailable"], bool):
        return None
    return dict(value)


@dataclass(frozen=True, slots=True)
class CacheState:
    mesh_revision: str
    written_at: int
    hosts: tuple[dict[str, object], ...]


class RemoteCache:
    """Private cache with permission, validation, and atomic-write boundaries."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        now_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self.directory = cache_directory() if directory is None else directory
        self._now_millis = now_millis

    @property
    def _state_path(self) -> Path:
        return self.directory / "remote-inventory-v1.json"

    @property
    def _lock_path(self) -> Path:
        return self.directory / "remote-inventory-v1.lock"

    @property
    def _refresh_lock_path(self) -> Path:
        return self.directory / "remote-refresh-v1.lock"

    @property
    def _marker_path(self) -> Path:
        return self.directory / "remote-refresh-v1.json"

    @contextmanager
    def lock(self, *, refresh: bool = False, blocking: bool = True) -> Iterator[bool]:
        _clean_directory(self.directory)
        path = self._refresh_lock_path if refresh else self._lock_path
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # ``O_CREAT`` observes the process umask only for a new file.  An
            # old lock can have weaker permissions, so repair it every time.
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)

    def _read_raw(self, path: Path) -> object | None:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data or len(data) > _MAX_BYTES:
            return None
        try:
            return json.loads(data)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None

    def _load_unlocked(self, snapshot: MeshSnapshot) -> CacheState | None:
        raw = self._read_raw(self._state_path)
        if not isinstance(raw, dict) or set(raw) != {
            "schemaVersion",
            "fingerprint",
            "meshRevision",
            "writtenAt",
            "hosts",
        }:
            return None
        fingerprint = f"remote-cache-v{_SCHEMA_VERSION}:{snapshot.revision}"
        if (
            raw.get("schemaVersion") != _SCHEMA_VERSION
            or raw.get("fingerprint") != fingerprint
            or raw.get("meshRevision") != snapshot.revision
            or not _number(raw.get("writtenAt"))
            or not isinstance(raw.get("hosts"), list)
            or len(raw["hosts"]) > _MAX_HOSTS
        ):
            return None
        allowed = {host.host_id for host in snapshot.hosts if not host.local}
        rows: list[dict[str, object]] = []
        for candidate in raw["hosts"]:
            row = _host(candidate)
            if row is None or row["hostId"] not in allowed:
                return None
            rows.append(row)
        if len({str(row["hostId"]) for row in rows}) != len(rows):
            return None
        return CacheState(snapshot.revision, raw["writtenAt"], tuple(rows))

    def load(self, snapshot: MeshSnapshot) -> CacheState | None:
        with self.lock() as acquired:
            return self._load_unlocked(snapshot) if acquired else None

    def _atomic_json(self, path: Path, payload: object) -> None:
        _clean_directory(self.directory)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _error(row: Mapping[str, object]) -> dict[str, object]:
        candidate = row.get("error")
        if isinstance(candidate, dict):
            return {
                "code": clean_message(candidate.get("code"), limit=120),
                "message": clean_message(candidate.get("message"), limit=480),
            }
        return {"code": "operation_failed", "message": "remote inventory did not complete"}

    def _merge_row(
        self, previous: dict[str, object] | None, current: Mapping[str, object]
    ) -> dict[str, object] | None:
        checked = _host(dict(current))
        if checked is None:
            return None
        status = checked["status"]
        if status == "ok":
            checked.pop("lastSeenAt", None)
            checked.pop("stale", None)
            checked.pop("unavailable", None)
            return checked
        if status == "tmux_missing":
            checked["sessions"] = []
            checked["serverGeneration"] = None
            checked["stale"] = False
            checked["unavailable"] = False
            checked["lastSeenAt"] = checked["observedAt"]
            return checked
        if previous is None:
            checked["stale"] = True
            checked["unavailable"] = True
            checked["sessions"] = [
                {**session, "attachedClients": None} for session in checked["sessions"]
            ]
            return checked
        retained = dict(previous)
        retained.update(
            {
                "status": status,
                "observedAt": checked["observedAt"],
                "route": checked["route"],
                "error": self._error(checked),
                "lastSeenAt": previous.get("lastSeenAt", previous["observedAt"]),
                "stale": True,
                "unavailable": True,
            }
        )
        if checked["nativeHostname"] is not None:
            retained["nativeHostname"] = checked["nativeHostname"]
        # Both transport and reached-domain failures leave only historical
        # rows.  Never render an old attachment count as a current fact.
        retained["sessions"] = [
            {**session, "attachedClients": None} for session in retained["sessions"]
        ]
        return retained

    @staticmethod
    def _live_row(candidate: Mapping[str, object]) -> dict[str, object]:
        """Validate a remote InventoryService row, never retained cache state."""
        if any(key in candidate for key in ("lastSeenAt", "stale", "unavailable")):
            raise ContractError("operation_failed", "remote refresh returned an invalid host row")
        checked = _host(dict(candidate))
        if (
            checked is None
            or (checked["status"] == "ok" and "error" in checked)
            or (checked["status"] != "ok" and ("error" not in checked or checked["sessions"]))
        ):
            raise ContractError("operation_failed", "remote refresh returned an invalid host row")
        return checked

    @classmethod
    def _current_rows(
        cls, snapshot: MeshSnapshot, current_rows: Sequence[Mapping[str, object]]
    ) -> dict[str, Mapping[str, object]]:
        """Validate one complete live remote response before any cache write."""
        expected = [host.host_id for host in snapshot.hosts if not host.local]
        if len(current_rows) != len(expected):
            raise ContractError("operation_failed", "remote refresh omitted or added a host row")
        current: dict[str, Mapping[str, object]] = {}
        for candidate in current_rows:
            if not isinstance(candidate, Mapping):
                raise ContractError(
                    "operation_failed", "remote refresh returned an invalid host row"
                )
            checked = cls._live_row(candidate)
            host_id = checked["hostId"]
            if host_id not in expected or host_id in current:
                raise ContractError("operation_failed", "remote refresh host rows are invalid")
            current[str(host_id)] = candidate
        if set(current) != set(expected):
            raise ContractError("operation_failed", "remote refresh omitted or added a host row")
        return current

    def merge(
        self, snapshot: MeshSnapshot, current_rows: Sequence[Mapping[str, object]]
    ) -> CacheState:
        """Atomically merge one revision-pinned remote result into private state."""
        current = self._current_rows(snapshot, current_rows)
        with self.lock() as acquired:
            if not acquired:  # blocking lock currently always acquires
                raise RuntimeError("remote cache lock unexpectedly unavailable")
            existing = self._load_unlocked(snapshot)
            previous = (
                {} if existing is None else {str(row["hostId"]): row for row in existing.hosts}
            )
            merged: list[dict[str, object]] = []
            for host in snapshot.hosts:
                if host.local or host.host_id not in current:
                    continue
                row = self._merge_row(previous.get(host.host_id), current[host.host_id])
                if row is not None:
                    merged.append(row)
            state = {
                "schemaVersion": _SCHEMA_VERSION,
                "fingerprint": f"remote-cache-v{_SCHEMA_VERSION}:{snapshot.revision}",
                "meshRevision": snapshot.revision,
                "writtenAt": self._now_millis(),
                "hosts": merged,
            }
            self._atomic_json(self._state_path, state)
            return CacheState(snapshot.revision, state["writtenAt"], tuple(merged))

    def merge_host(
        self, snapshot: MeshSnapshot, host_id: str, current_row: Mapping[str, object]
    ) -> CacheState:
        """Atomically merge exactly one live remote host without touching peers.

        A post-mutation reconciliation must not transform into an all-host SSH
        refresh. Retain every other valid cached row in Mesh order and keep the
        old state timestamp, so updating one host cannot make peers look fresh.
        """
        remote_ids = {host.host_id for host in snapshot.hosts if not host.local}
        if host_id not in remote_ids:
            raise ContractError("invalid_input", "remote cache host is invalid")
        checked = self._live_row(current_row)
        if checked["hostId"] != host_id:
            raise ContractError("operation_failed", "remote refresh host row is mismatched")
        with self.lock() as acquired:
            if not acquired:  # blocking lock currently always acquires
                raise RuntimeError("remote cache lock unexpectedly unavailable")
            existing = self._load_unlocked(snapshot)
            previous = (
                {} if existing is None else {str(row["hostId"]): row for row in existing.hosts}
            )
            merged: list[dict[str, object]] = []
            for host in snapshot.hosts:
                if host.local:
                    continue
                if host.host_id == host_id:
                    row = self._merge_row(previous.get(host_id), checked)
                    assert row is not None
                    merged.append(row)
                elif host.host_id in previous:
                    merged.append(previous[host.host_id])
            written_at = existing.written_at if existing is not None else self._now_millis()
            state = {
                "schemaVersion": _SCHEMA_VERSION,
                "fingerprint": f"remote-cache-v{_SCHEMA_VERSION}:{snapshot.revision}",
                "meshRevision": snapshot.revision,
                "writtenAt": written_at,
                "hosts": merged,
            }
            self._atomic_json(self._state_path, state)
            return CacheState(snapshot.revision, written_at, tuple(merged))

    def write_marker(self, state: str, mesh_revision: str, *, message: str | None = None) -> None:
        if state not in {"running", "complete", "failed", "stale"}:
            raise ValueError("invalid remote refresh state")
        if not _text(mesh_revision) or not mesh_revision:
            raise ValueError("invalid remote refresh mesh revision")
        payload: dict[str, object] = {
            "schemaVersion": _SCHEMA_VERSION,
            "state": state,
            "meshRevision": mesh_revision,
            "updatedAt": self._now_millis(),
        }
        if message is not None:
            payload["message"] = clean_message(message)
        self._atomic_json(self._marker_path, payload)

    def marker(self, *, mesh_revision: str, stall_after_seconds: int) -> dict[str, object] | None:
        raw = self._read_raw(self._marker_path)
        if (
            not isinstance(raw, dict)
            or not {
                "schemaVersion",
                "state",
                "meshRevision",
                "updatedAt",
            }
            <= set(raw)
            or set(raw) - {"schemaVersion", "state", "meshRevision", "updatedAt", "message"}
        ):
            return None
        if (
            raw.get("schemaVersion") != _SCHEMA_VERSION
            or raw.get("state") not in {"running", "complete", "failed", "stale"}
            or raw.get("meshRevision") != mesh_revision
            or not _text(raw.get("meshRevision"))
            or not _number(raw.get("updatedAt"))
            or ("message" in raw and not _text(raw["message"]))
        ):
            return None
        result = dict(raw)
        if (
            result["state"] == "running"
            and self._now_millis() - result["updatedAt"] > stall_after_seconds * 1000
        ):
            result["state"] = "stalled"
        return result
