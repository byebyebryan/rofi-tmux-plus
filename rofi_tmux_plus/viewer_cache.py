"""Private, endpoint-scoped cache for local viewer observations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .remote_cache import cache_directory

VIEWER_FRESHNESS_SECONDS = 10
LOCAL_ONLY_REVISION = "local-only"
_SCHEMA_VERSION = 1
_MAX_BYTES = 4 * 1024 * 1024
_MAX_SESSIONS = 128 * 256
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,4095}$", re.ASCII)
_SESSION_ID = re.compile(r"^\$[0-9]+$", re.ASCII)
_GENERATION = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,4096}$")
_REASONS = frozenset(
    {
        "unsupported_desktop",
        "compositor_unavailable",
        "process_unavailable",
        "ambiguous_match",
        "conflicting_metadata",
        "pending_registration",
        "attachment_unverified",
        "inventory_incomplete",
    }
)


def desktop_context_id(
    environ: Mapping[str, str] | None = None,
    *,
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
    stat_path: Callable[[str], os.stat_result] = os.stat,
) -> str:
    """Hash the current boot and desktop socket/display identity without storing it."""
    env = os.environ if environ is None else environ
    try:
        boot_id = boot_id_path.read_text(encoding="ascii").strip()[:128]
    except OSError:
        boot_id = None
    socket_path = env.get("NIRI_SOCKET")
    socket_identity: tuple[int, int, int] | None = None
    if socket_path:
        try:
            info = stat_path(socket_path)
            socket_identity = (info.st_dev, info.st_ino, info.st_ctime_ns)
        except OSError:
            socket_identity = None
    value = {
        "bootId": boot_id,
        "niriSocket": socket_path,
        "niriSocketIdentity": socket_identity,
        "waylandDisplay": env.get("WAYLAND_DISPLAY"),
        "display": env.get("DISPLAY"),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    return hashlib.sha256(encoded).hexdigest()


def _reference(value: object) -> tuple[str, str, str, int] | None:
    if not isinstance(value, Mapping):
        return None
    host_id = value.get("hostId")
    generation = value.get("serverGeneration")
    session_id = value.get("sessionId")
    created_at = value.get("createdAt")
    if (
        not isinstance(host_id, str)
        or _HOST.fullmatch(host_id) is None
        or not isinstance(generation, str)
        or _GENERATION.fullmatch(generation) is None
        or not isinstance(session_id, str)
        or _SESSION_ID.fullmatch(session_id) is None
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or not 0 <= created_at <= 2**63 - 1
    ):
        return None
    return host_id, generation, session_id, created_at


def _observation(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    state = value.get("state")
    confidence = value.get("confidence")
    reason = value.get("reason")
    if (
        state == "open"
        and isinstance(confidence, str)
        and confidence in {"confirmed", "matched"}
        and reason is None
    ):
        return {"state": "open", "confidence": confidence}
    if state == "none" and confidence is None and reason is None:
        return {"state": "none"}
    if (
        state == "unknown"
        and confidence is None
        and (reason is None or isinstance(reason, str) and reason in _REASONS)
    ):
        result = {"state": "unknown"}
        if isinstance(reason, str):
            result["reason"] = reason
        return result
    return None


def _cache_key(reference: tuple[str, str, str, int]) -> str:
    return json.dumps(reference, separators=(",", ":"), ensure_ascii=True)


class ViewerObservationCache:
    """Stores only current, complete-reference viewer observations for one endpoint."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        now_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        context_id: Callable[[], str] = desktop_context_id,
    ) -> None:
        self.directory = cache_directory() if directory is None else Path(directory)
        self._now_millis = now_millis
        self._context_id = context_id

    @property
    def _path(self) -> Path:
        return self.directory / "viewer-observations-v1.json"

    def context_id(self) -> str:
        return self._context_id()

    @staticmethod
    def _mesh_revision(value: object) -> bool:
        return value is None or (
            isinstance(value, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", value, re.ASCII) is not None
        )

    def _read(self, mesh_revision: str | None, context_id: str) -> dict[str, object] | None:
        descriptor = -1
        try:
            descriptor = os.open(
                self._path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > _MAX_BYTES
            ):
                return None
            raw = os.read(descriptor, _MAX_BYTES + 1)
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not raw or len(raw) > _MAX_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schemaVersion",
                "meshRevision",
                "endpointContext",
                "viewerEndpoint",
                "sessions",
            }
            or type(value.get("schemaVersion")) is not int
            or value.get("schemaVersion") != _SCHEMA_VERSION
            or value.get("meshRevision") != mesh_revision
            or value.get("endpointContext") != context_id
            or not isinstance(value.get("sessions"), list)
            or len(value["sessions"]) > _MAX_SESSIONS
        ):
            return None
        endpoint = value.get("viewerEndpoint")
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"hostId", "observedAt"}
            or not isinstance(endpoint.get("hostId"), str)
            or _HOST.fullmatch(endpoint["hostId"]) is None
            or not isinstance(endpoint.get("observedAt"), int)
            or isinstance(endpoint.get("observedAt"), bool)
            or not 0 <= endpoint["observedAt"] <= 2**63 - 1
        ):
            return None
        seen: set[str] = set()
        for row in value["sessions"]:
            if not isinstance(row, dict) or set(row) != {
                "hostId",
                "serverGeneration",
                "sessionId",
                "createdAt",
                "localViewer",
            }:
                return None
            ref = _reference(row)
            observation = _observation(row.get("localViewer"))
            if ref is None or observation is None:
                return None
            key = _cache_key(ref)
            if key in seen:
                return None
            seen.add(key)
        return value

    def _write(self, value: Mapping[str, object]) -> None:
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("viewer observation cache exceeds its size limit")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".viewer-observations-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = ""
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def merge_response(
        self,
        response: Mapping[str, object],
        *,
        mesh_revision: str | None,
        expected_context: str,
        endpoint_host_id: str,
        expected_host_ids: Sequence[str],
    ) -> bool:
        """Publish a complete bulk observation only while its endpoint scope is current."""
        if not self._mesh_revision(mesh_revision) or self.context_id() != expected_context:
            return False
        if response.get("schemaVersion") != 1 or response.get("meshRevision") != mesh_revision:
            return False
        endpoint = response.get("viewerEndpoint")
        hosts = response.get("hosts")
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"hostId", "observedAt"}
            or not isinstance(endpoint.get("hostId"), str)
            or _HOST.fullmatch(endpoint["hostId"]) is None
            or endpoint.get("hostId") != endpoint_host_id
            or not isinstance(endpoint.get("observedAt"), int)
            or isinstance(endpoint.get("observedAt"), bool)
            or not 0 <= endpoint["observedAt"] <= 2**63 - 1
            or not isinstance(hosts, list)
            or len(hosts) > 128
            or len(expected_host_ids) > 128
            or len(hosts) != len(expected_host_ids)
            or any(
                not isinstance(host_id, str) or _HOST.fullmatch(host_id) is None
                for host_id in expected_host_ids
            )
            or len(set(expected_host_ids)) != len(expected_host_ids)
        ):
            return False
        if {host.get("hostId") for host in hosts if isinstance(host, Mapping)} != set(
            expected_host_ids
        ):
            return False
        observed_at = endpoint["observedAt"]
        previous = self._read(mesh_revision, expected_context)
        sessions: dict[str, dict[str, object]] = {}
        if previous is not None:
            old_endpoint = previous["viewerEndpoint"]
            assert isinstance(old_endpoint, dict)
            if observed_at < old_endpoint["observedAt"]:
                return False

        for host in hosts:
            if not isinstance(host, Mapping) or not isinstance(host.get("hostId"), str):
                return False
            host_id = host["hostId"]
            if _HOST.fullmatch(host_id) is None:
                return False
            rows = host.get("sessions")
            if not isinstance(rows, list):
                return False
            if host.get("status") == "ok":
                sessions = {
                    key: row for key, row in sessions.items() if row.get("hostId") != host_id
                }
            elif host.get("status") not in {"unreachable", "tmux_missing", "error"}:
                return False
            for row in rows:
                if not isinstance(row, Mapping):
                    return False
                reference = _reference(row)
                if reference is None or reference[0] != host_id:
                    return False
                observation = _observation(row.get("localViewer"))
                if observation is None:
                    if host.get("status") == "ok":
                        observation = {"state": "unknown", "reason": "inventory_incomplete"}
                    else:
                        continue
                normalized = {
                    "hostId": reference[0],
                    "serverGeneration": reference[1],
                    "sessionId": reference[2],
                    "createdAt": reference[3],
                    "localViewer": observation,
                }
                sessions[_cache_key(reference)] = normalized
            if host.get("status") != "ok":
                for row in sessions.values():
                    if row.get("hostId") == host_id:
                        row["localViewer"] = {
                            "state": "unknown",
                            "reason": "inventory_incomplete",
                        }

        if len(sessions) > _MAX_SESSIONS or self.context_id() != expected_context:
            return False
        self._write(
            {
                "schemaVersion": _SCHEMA_VERSION,
                "meshRevision": mesh_revision,
                "endpointContext": expected_context,
                "viewerEndpoint": {
                    "hostId": endpoint["hostId"],
                    "observedAt": observed_at,
                },
                "sessions": list(sessions.values()),
            }
        )
        return True

    def decorate(
        self,
        payload: dict[str, object],
        *,
        mesh_revision: str | None,
        endpoint_host_id: str,
    ) -> bool:
        """Join fresh observations onto current session rows, failing expired rows to Unknown."""
        context = self.context_id()
        state = self._read(mesh_revision, context)
        if state is not None:
            endpoint = state.get("viewerEndpoint")
            if not isinstance(endpoint, dict) or endpoint.get("hostId") != endpoint_host_id:
                state = None
        now = self._now_millis()
        endpoint = None if state is None else state.get("viewerEndpoint")
        observed_at = None
        entries: dict[str, dict[str, object]] = {}
        if isinstance(endpoint, dict):
            observed_at = endpoint.get("observedAt")
            for row in state["sessions"]:
                assert isinstance(row, dict)
                reference = _reference(row)
                assert reference is not None
                entries[_cache_key(reference)] = row
        fresh = (
            isinstance(observed_at, int)
            and not isinstance(observed_at, bool)
            and 0 <= now - observed_at < VIEWER_FRESHNESS_SECONDS * 1000
        )
        needed = False
        hosts = payload.get("hosts")
        if isinstance(hosts, list):
            for host in hosts:
                if not isinstance(host, dict):
                    needed = True
                    continue
                sessions = host.get("sessions")
                if not isinstance(sessions, list):
                    needed = True
                    continue
                for session in sessions:
                    if not isinstance(session, dict):
                        needed = True
                        continue
                    reference = _reference(session)
                    key = None if reference is None else _cache_key(reference)
                    row = entries.get(key) if key is not None else None
                    observation = _observation(row.get("localViewer")) if row is not None else None
                    if not fresh:
                        session["localViewer"] = {
                            "state": "unknown",
                            "reason": "inventory_incomplete",
                        }
                        needed = True
                    elif host.get("status") != "ok":
                        # The endpoint scan completed, but this owner host has
                        # no current inventory. Keep historical rows Unknown
                        # until the next normal ten-second viewer refresh.
                        session["localViewer"] = {
                            "state": "unknown",
                            "reason": "inventory_incomplete",
                        }
                    elif observation is None:
                        session["localViewer"] = {
                            "state": "unknown",
                            "reason": "inventory_incomplete",
                        }
                        needed = True
                    else:
                        session["localViewer"] = observation
        if isinstance(endpoint, dict):
            payload["viewerEndpoint"] = dict(endpoint)
            payload["viewerObservedAt"] = observed_at
        else:
            payload.pop("viewerEndpoint", None)
            payload["viewerObservedAt"] = None
        payload["viewerEndpointHostId"] = endpoint_host_id
        payload["viewerRefreshNeeded"] = needed
        return needed
