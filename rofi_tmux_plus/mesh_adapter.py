"""Strict process-only Host Mesh v1 consumer boundary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .bounded_process import BoundedCompleted, run_bounded
from .errors import ContractError, clean_message

_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)
_MAX_OUTPUT = 512 * 1024


def _clean_token(value: object, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or value.startswith("-"):
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    if any(char.isspace() or unicodedata.category(char).startswith("C") for char in value):
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    if identifier and not _HOST_ID.fullmatch(value):
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    return value


def _clean_display(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError("operation_failed", f"Host Mesh {label} is invalid")
    return value


def _timestamp(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label, 0, 2**63 - 1)


@dataclass(frozen=True, slots=True)
class MeshPolicy:
    executable: str
    connect_timeout_seconds: int
    connection_attempts: int
    route_health_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class MeshRoute:
    destination: str
    configured_index: int
    last_reachable_at: int | None
    last_unreachable_at: int | None


@dataclass(frozen=True, slots=True)
class MeshHost:
    host_id: str
    display: str
    local: bool
    aliases: tuple[str, ...]
    routes: tuple[MeshRoute, ...]


@dataclass(frozen=True, slots=True)
class MeshSnapshot:
    revision: str
    local_host_id: str
    policy: MeshPolicy
    hosts: tuple[MeshHost, ...]

    @property
    def local_host(self) -> MeshHost:
        return next(host for host in self.hosts if host.local)

    def resolve_host(self, value: str) -> MeshHost:
        key = value.casefold()
        matches = [
            host
            for host in self.hosts
            if key == host.host_id.casefold()
            or any(key == alias.casefold() for alias in host.aliases)
        ]
        if len(matches) != 1:
            raise ContractError(
                "unknown_host", f"unknown Host Mesh host: {clean_message(value)}", value
            )
        return matches[0]


class MeshStaleError(ContractError):
    def __init__(self) -> None:
        super().__init__("stale_mesh", "the Host Mesh changed; refresh and try again")


class HostMeshAdapter:
    """Consumes only the published executable contract, never SSH Plus files."""

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str] | BoundedCompleted] | None = None,
    ) -> None:
        self._which = which
        self._runner = runner

    def _executable(self) -> str | None:
        return self._which("rofi-ssh-plus")

    def load(self) -> MeshSnapshot | None:
        executable = self._executable()
        if executable is None:
            return None
        payload = self._run_json(executable, ["mesh", "list", "--json"])
        return _parse_snapshot(payload)

    def report_route(
        self,
        *,
        host_id: str,
        route: str,
        status: str,
        mesh_revision: str,
        observed_at: int,
        timeout_seconds: float = 5,
    ) -> bool:
        executable = self._executable()
        if executable is None:
            raise ContractError(
                "operation_failed", "Host Mesh provider disappeared during route report"
            )
        payload = self._run_json(
            executable,
            [
                "mesh",
                "report-route",
                "--json",
                "--host",
                host_id,
                "--route",
                route,
                "--status",
                status,
                "--source",
                "rofi-tmux-plus",
                "--mesh-revision",
                mesh_revision,
                "--observed-at",
                str(observed_at),
            ],
            timeout_seconds=timeout_seconds,
        )
        if (
            isinstance(payload, dict)
            and payload.get("schemaVersion") == 1
            and payload.get("ok") is False
        ):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("code") == "stale_mesh":
                raise MeshStaleError()
            raise ContractError("operation_failed", "Host Mesh rejected route health report")
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 1
            or payload.get("ok") is not True
            or not isinstance(payload.get("accepted"), bool)
        ):
            raise ContractError(
                "operation_failed", "Host Mesh returned an invalid route report response"
            )
        return payload["accepted"]

    def _run_json(
        self, executable: str, args: Sequence[str], *, timeout_seconds: float = 5
    ) -> object:
        try:
            if self._runner is None:
                completed = run_bounded(
                    [executable, *args],
                    timeout=timeout_seconds,
                    stdout_limit=_MAX_OUTPUT,
                    stderr_limit=_MAX_OUTPUT,
                )
            else:
                completed = self._runner(
                    [executable, *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=timeout_seconds,
                )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise ContractError(
                "operation_failed", f"Host Mesh command failed: {clean_message(error)}"
            ) from error
        if getattr(completed, "timed_out", False):
            raise ContractError("operation_failed", "Host Mesh command timed out")
        if getattr(completed, "overflow_streams", frozenset()):
            raise ContractError(
                "operation_failed", "Host Mesh response exceeded the consumer limit"
            )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if (
            len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT
            or len(stderr.encode("utf-8", errors="replace")) > _MAX_OUTPUT
        ):
            raise ContractError(
                "operation_failed", "Host Mesh response exceeded the consumer limit"
            )
        try:
            payload = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise ContractError("operation_failed", "Host Mesh returned malformed JSON") from error
        if completed.returncode != 0:
            if isinstance(payload, dict) and payload.get("schemaVersion") == 1:
                error = payload.get("error")
                if isinstance(error, dict) and error.get("code") == "stale_mesh":
                    raise MeshStaleError()
            raise ContractError("operation_failed", "Host Mesh command returned a failure")
        return payload


def _parse_snapshot(payload: object) -> MeshSnapshot:
    if not isinstance(payload, dict):
        raise ContractError("operation_failed", "Host Mesh list response must be an object")
    if payload.get("schemaVersion") != 1:
        raise ContractError("operation_failed", "unsupported Host Mesh schema version")
    revision = _clean_token(payload.get("meshRevision"), "meshRevision")
    _integer(payload.get("generatedAt"), "generatedAt", 0, 2**63 - 1)
    local_host_id = _clean_token(
        payload.get("localHostId"), "localHostId", identifier=True
    ).casefold()
    raw_policy = payload.get("sshPolicy")
    if not isinstance(raw_policy, dict):
        raise ContractError("operation_failed", "Host Mesh sshPolicy is invalid")
    policy = MeshPolicy(
        _clean_token(raw_policy.get("executable"), "ssh executable"),
        _integer(raw_policy.get("connectTimeoutSeconds"), "connectTimeoutSeconds", 1, 60),
        _integer(raw_policy.get("connectionAttempts"), "connectionAttempts", 1, 10),
        _integer(raw_policy.get("routeHealthTtlSeconds"), "routeHealthTtlSeconds", 1, 86400),
    )
    raw_hosts = payload.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ContractError("operation_failed", "Host Mesh hosts is invalid")
    hosts: list[MeshHost] = []
    identity_owners: dict[str, str] = {}
    local_count = 0
    for index, raw_host in enumerate(raw_hosts):
        if not isinstance(raw_host, dict) or not isinstance(raw_host.get("local"), bool):
            raise ContractError("operation_failed", "Host Mesh host is invalid")
        host_id = _clean_token(raw_host.get("id"), "host id", identifier=True).casefold()
        display = _clean_display(raw_host.get("display"), "host display")
        raw_aliases = raw_host.get("aliases")
        if not isinstance(raw_aliases, list):
            raise ContractError("operation_failed", "Host Mesh aliases is invalid")
        aliases = tuple(_clean_token(alias, "host alias") for alias in raw_aliases)
        raw_routes = raw_host.get("routes")
        if not isinstance(raw_routes, list):
            raise ContractError("operation_failed", "Host Mesh routes is invalid")
        routes: list[MeshRoute] = []
        configured_indices: set[int] = set()
        for raw_route in raw_routes:
            if not isinstance(raw_route, dict):
                raise ContractError("operation_failed", "Host Mesh route is invalid")
            configured_index = _integer(
                raw_route.get("configuredIndex"), "configuredIndex", 0, 2**31 - 1
            )
            if configured_index in configured_indices:
                raise ContractError(
                    "operation_failed", "Host Mesh configured route indices are ambiguous"
                )
            configured_indices.add(configured_index)
            routes.append(
                MeshRoute(
                    _clean_token(raw_route.get("destination"), "route destination"),
                    configured_index,
                    _timestamp(raw_route.get("lastReachableAt"), "lastReachableAt"),
                    _timestamp(raw_route.get("lastUnreachableAt"), "lastUnreachableAt"),
                )
            )
        if raw_host["local"]:
            local_count += 1
            if index != 0 or host_id != local_host_id or routes:
                raise ContractError("operation_failed", "Host Mesh local host is invalid")
        elif not routes:
            raise ContractError("operation_failed", "Host Mesh remote host has no routes")
        for identity in (host_id, *aliases, *(route.destination for route in routes)):
            key = identity.casefold()
            owner = identity_owners.get(key)
            if owner is not None and owner != host_id:
                raise ContractError("operation_failed", "Host Mesh identities are ambiguous")
            identity_owners[key] = host_id
        hosts.append(MeshHost(host_id, display, raw_host["local"], aliases, tuple(routes)))
    if local_count != 1:
        raise ContractError("operation_failed", "Host Mesh must declare exactly one local host")
    return MeshSnapshot(revision, local_host_id, policy, tuple(hosts))
