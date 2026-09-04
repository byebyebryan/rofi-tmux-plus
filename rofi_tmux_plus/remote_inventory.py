"""Nonce-marked, process-only remote tmux inventory for Host Mesh v1."""

from __future__ import annotations

import re
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .bounded_process import BoundedCompleted, run_bounded
from .errors import ContractError, clean_message
from .mesh_adapter import HostMeshAdapter, MeshHost, MeshPolicy, MeshStaleError
from .model import Pane, Session, SessionReference
from .tmux import validate_session_id, validate_user_option

_MARKER_PREFIX = "\x1eROFI_PLUS_REACHED_V1:"
_MARKER_SUFFIX = "\x1f\n"
_TRANSPORT_MARKERS = (
    "could not resolve hostname",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "connection reset by peer",
    "kex_exchange_identification",
)
_MAX_OUTPUT = 1024 * 1024
_MAX_LINE = 64 * 1024
_MAX_FIELD = 16 * 1024
_MAX_SESSIONS = 256
_MAX_PANES = 512
_MAX_NUMBER = 2**63 - 1
_HEX = re.compile(r"^[0-9A-Fa-f]*$", re.ASCII)
_PANE_ID = re.compile(r"^%[0-9]+$", re.ASCII)
_NATIVE_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$", re.ASCII)


# This program intentionally contains no dynamic request value. The reached
# marker is emitted by the outer wrapper; all options are positional arguments
# and every tmux field is hex framed so paths/names cannot inject records.
_REMOTE_PROGRAM = r"""set -eu
hex() { LC_ALL=C od -An -v -tx1 | tr -d ' \n'; }
field() { printf '\t'; "$@" | hex; }
literal() { printf '\t'; printf '%s' "$1" | hex; }
option_present() {
  tmux show-options -q -t "$1" | awk -v opt="$2" '$0 == opt || index($0, opt " ") == 1 { found=1 } END { exit !found }'
}
panes_flag=$1
shift
native_hostname=$(hostname 2>/dev/null || uname -n 2>/dev/null || :)
printf H
literal "$native_hostname"
printf '\n'
if ! command -v tmux >/dev/null 2>&1; then printf 'T\tM\n'; exit 0; fi
if tmux display-message -p '#{pid}' >/dev/null 2>&1; then :
elif tmux display-message -p '#{pid}' 2>&1 | grep -E -q 'no server running|failed to connect to server|error connecting to'; then
  printf 'T\tN\n'
  exit 0
else
  printf E
  literal 'tmux server probe failed'
  printf '\n'
  exit 0
fi
printf G
field tmux display-message -p '#{socket_path}'
field tmux display-message -p '#{start_time}'
field tmux display-message -p '#{pid}'
printf '\n'
tmux list-sessions -F '#{session_id}' | while IFS= read -r sid; do
  [ -n "$sid" ] || continue
  printf D
  literal "$sid"
  field tmux display-message -p -t "$sid" '#{session_created}'
  field tmux display-message -p -t "$sid" '#{session_name}'
  field tmux display-message -p -t "$sid" '#{session_activity}'
  field tmux display-message -p -t "$sid" '#{session_last_attached}'
  field tmux display-message -p -t "$sid" '#{session_attached}'
  field tmux display-message -p -t "$sid" '#{session_windows}'
  field tmux display-message -p -t "$sid" '#{session_path}'
  field tmux display-message -p -t "$sid" '#{window_name}'
  field tmux display-message -p -t "$sid" '#{pane_current_path}'
  if option_present "$sid" '@rofi_tmux_plus_pending'; then literal 1; else literal 0; fi
  printf '\n'
  for opt in "$@"; do
    printf O
    literal "$sid"
    literal "$opt"
    if option_present "$sid" "$opt"; then field tmux show-options -qv -t "$sid" "$opt"; else printf '\t-'; fi
    printf '\n'
  done
  if [ "$panes_flag" = 1 ]; then
    tmux list-panes -s -t "$sid" -F '#{pane_id}' | while IFS= read -r pane; do
      [ -n "$pane" ] || continue
      printf P
      literal "$sid"
      literal "$pane"
      field tmux display-message -p -t "$pane" '#{pane_pid}'
      field tmux display-message -p -t "$pane" '#{pane_current_path}'
      field tmux display-message -p -t "$pane" '#{pane_current_command}'
      printf '\n'
    done
  fi
done"""


def generate_nonce() -> str:
    return secrets.token_hex(16)


def _marker(nonce: str) -> str:
    return f"{_MARKER_PREFIX}{nonce}{_MARKER_SUFFIX}"


def parse_reached_marker(stderr: str, nonce: str) -> tuple[bool, str]:
    marker = _marker(nonce)
    if stderr.count(marker) != 1:
        return False, stderr
    return True, stderr.replace(marker, "", 1)


def _transport_failure(stderr: str, *, timed_out: bool) -> bool:
    if timed_out:
        return True
    lowered = stderr.casefold()
    return any(marker in lowered for marker in _TRANSPORT_MARKERS)


def _drop_output_newline(value: bytes) -> bytes:
    return value[:-1] if value.endswith(b"\n") else value


def _decode_field(value: str) -> str:
    if len(value) > _MAX_FIELD * 2 or len(value) % 2 or not _HEX.fullmatch(value):
        raise ContractError("operation_failed", "remote tmux field exceeds the framing limit")
    try:
        raw = bytes.fromhex(value)
    except ValueError as error:
        raise ContractError("operation_failed", "remote tmux framing is invalid") from error
    raw = _drop_output_newline(raw)
    if len(raw) > _MAX_FIELD:
        raise ContractError("operation_failed", "remote tmux field exceeds the framing limit")
    return raw.decode("utf-8", errors="replace")


def _number(value: str, *, nullable: bool = False) -> int | None:
    if nullable and value == "":
        return None
    if not value.isascii() or not value.isdecimal():
        raise ContractError("operation_failed", "remote tmux emitted an invalid numeric field")
    try:
        number = int(value)
    except ValueError as error:
        raise ContractError(
            "operation_failed", "remote tmux emitted an invalid numeric field"
        ) from error
    if number > _MAX_NUMBER:
        raise ContractError("operation_failed", "remote tmux emitted an invalid numeric field")
    return number


def _native_hostname(value: str) -> str | None:
    return value if _NATIVE_HOSTNAME.fullmatch(value) else None


@dataclass(slots=True)
class _SessionParts:
    created_at: int
    name: str | None
    activity_at: int | None
    last_attached_at: int | None
    attached_clients: int | None
    pending: bool
    window_count: int | None
    session_path: str | None
    current_window: str | None
    current_path: str | None
    options: dict[str, str | None]
    panes: list[Pane]


@dataclass(frozen=True, slots=True)
class ParsedRemoteInventory:
    generation: str | None
    sessions: tuple[Session, ...]
    status: str | None
    native_hostname: str | None
    error_message: str | None = None


def parse_remote_inventory(
    output: str,
    *,
    host_id: str,
    panes_requested: bool,
    option_names: Sequence[str],
) -> ParsedRemoteInventory:
    """Parse fixed remote framing without allowing tmux fields to delimit records."""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_OUTPUT:
        raise ContractError("operation_failed", "remote tmux output exceeded the consumer limit")
    generation: str | None = None
    records: dict[str, _SessionParts] = {}
    seen_generation = False
    seen_native_hostname = False
    native_hostname: str | None = None
    status: str | None = None
    error_message: str | None = None
    pane_count = 0
    for line in output.splitlines():
        if len(line.encode("utf-8", errors="replace")) > _MAX_LINE:
            raise ContractError(
                "operation_failed", "remote tmux record exceeded the consumer limit"
            )
        parts = line.split("\t")
        kind = parts[0]
        if kind == "H":
            if (
                len(parts) != 2
                or seen_native_hostname
                or seen_generation
                or status is not None
                or records
            ):
                raise ContractError("operation_failed", "remote tmux hostname framing is invalid")
            native_hostname = _native_hostname(_decode_field(parts[1]))
            seen_native_hostname = True
            continue
        if kind == "T":
            if (
                len(parts) != 2
                or parts[1] not in {"M", "N"}
                or not seen_native_hostname
                or seen_generation
                or status is not None
                or records
            ):
                raise ContractError("operation_failed", "remote tmux status framing is invalid")
            status = "tmux_missing" if parts[1] == "M" else "no_server"
            continue
        if kind == "E":
            if (
                len(parts) != 2
                or not seen_native_hostname
                or seen_generation
                or status is not None
                or records
            ):
                raise ContractError("operation_failed", "remote tmux error framing is invalid")
            status = "tmux_error"
            error_message = clean_message(_decode_field(parts[1]), limit=240)
            continue
        if kind == "G":
            if len(parts) != 4 or not seen_native_hostname or seen_generation or status is not None:
                raise ContractError("operation_failed", "remote tmux server framing is invalid")
            socket_path, started, pid = (_decode_field(value) for value in parts[1:])
            if not socket_path:
                raise ContractError("operation_failed", "remote tmux server identity is invalid")
            _number(started)
            _number(pid)
            generation = f"tmux-v1:{started}:{pid}:{socket_path}"
            seen_generation = True
            continue
        if kind == "D":
            if len(parts) != 12 or not seen_generation or status is not None:
                raise ContractError("operation_failed", "remote tmux session framing is invalid")
            (
                session_id,
                created,
                name,
                activity,
                last,
                attached,
                windows,
                path,
                current_window,
                current_path,
                pending,
            ) = (_decode_field(value) for value in parts[1:])
            try:
                validate_session_id(session_id)
            except ContractError as error:
                raise ContractError(
                    "operation_failed", "remote tmux session identity is invalid"
                ) from error
            if session_id in records or len(records) >= _MAX_SESSIONS or pending not in {"0", "1"}:
                raise ContractError("operation_failed", "remote tmux session framing is invalid")
            created_at = _number(created)
            assert created_at is not None
            records[session_id] = _SessionParts(
                created_at,
                name or None,
                _number(activity, nullable=True),
                _number(last, nullable=True),
                _number(attached, nullable=True),
                pending == "1",
                _number(windows, nullable=True),
                path or None,
                current_window or None,
                current_path or None,
                {},
                [],
            )
            continue
        if kind == "O":
            if len(parts) != 4 or not seen_generation or status is not None:
                raise ContractError("operation_failed", "remote tmux option framing is invalid")
            session_id, name = (_decode_field(value) for value in parts[1:3])
            if (
                session_id not in records
                or name not in option_names
                or name in records[session_id].options
            ):
                raise ContractError("operation_failed", "remote tmux option framing is invalid")
            records[session_id].options[name] = None if parts[3] == "-" else _decode_field(parts[3])
            continue
        if kind == "P":
            if len(parts) != 6 or not seen_generation or status is not None:
                raise ContractError("operation_failed", "remote tmux pane framing is invalid")
            session_id, pane_id, pid, current_path, command = (
                _decode_field(value) for value in parts[1:]
            )
            if (
                not panes_requested
                or session_id not in records
                or pane_count >= _MAX_PANES
                or not _PANE_ID.fullmatch(pane_id)
            ):
                raise ContractError("operation_failed", "remote tmux pane framing is invalid")
            records[session_id].panes.append(
                Pane(pane_id, _number(pid, nullable=True), current_path or None, command or None)
            )
            pane_count += 1
            continue
        raise ContractError("operation_failed", "remote tmux emitted an unknown framing record")
    if status is not None:
        return ParsedRemoteInventory(None, (), status, native_hostname, error_message)
    if not seen_generation:
        raise ContractError("operation_failed", "remote tmux output omitted server identity")
    assert generation is not None
    sessions: list[Session] = []
    for session_id, parts in records.items():
        if set(parts.options) != set(option_names):
            raise ContractError("operation_failed", "remote tmux output omitted a requested option")
        sessions.append(
            Session(
                SessionReference(host_id, generation, session_id, parts.created_at),
                parts.name,
                parts.activity_at,
                parts.last_attached_at,
                parts.attached_clients,
                parts.pending,
                parts.window_count,
                parts.session_path,
                parts.current_window,
                parts.current_path,
                tuple(parts.panes) if panes_requested else None,
                parts.options if option_names else None,
            )
        )
    return ParsedRemoteInventory(generation, tuple(sessions), None, native_hostname)


def build_remote_inventory_argv(
    route: str,
    policy: MeshPolicy,
    *,
    panes: bool,
    option_names: Sequence[str],
    nonce: str,
) -> list[str]:
    if len(nonce) < 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid reached-host nonce")
    for name in option_names:
        validate_user_option(name)
    marker_wrapper = r'''printf '\036ROFI_PLUS_REACHED_V1:%s\037\n' "$1" >&2
shift
exec "$@"'''
    domain = [
        "sh",
        "-c",
        _REMOTE_PROGRAM,
        "rofi-tmux-plus-remote",
        "1" if panes else "0",
        *option_names,
    ]
    remote = " ".join(
        shlex.quote(value)
        for value in ("sh", "-c", marker_wrapper, "rofi-plus-reached", nonce, *domain)
    )
    return [
        policy.executable,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={policy.connect_timeout_seconds}",
        "-o",
        f"ConnectionAttempts={policy.connection_attempts}",
        route,
        remote,
    ]


class RemoteInventory:
    def __init__(
        self,
        adapter: HostMeshAdapter,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str] | BoundedCompleted] | None = None,
        nonce_factory: Callable[[], str] = generate_nonce,
        now_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._adapter = adapter
        self._runner = runner
        self._nonce_factory = nonce_factory
        self._now_millis = now_millis

    def _run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str] | BoundedCompleted:
        if self._runner is None:
            return run_bounded(
                argv,
                timeout=timeout,
                stdout_limit=_MAX_OUTPUT,
                stderr_limit=_MAX_OUTPUT,
            )
        return self._runner(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )

    def inventory(
        self,
        host: MeshHost,
        policy: MeshPolicy,
        mesh_revision: str,
        *,
        panes: bool,
        option_names: Sequence[str],
        deadline: float,
    ) -> dict[str, object]:
        last_transport = "no configured route completed"
        last_unclassified = "remote SSH command did not prove a reached host"
        saw_unclassified = False
        for route in host.routes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._row(
                    host,
                    "error",
                    None,
                    [],
                    None,
                    "operation_failed",
                    "remote inventory deadline exceeded",
                )
            nonce = self._nonce_factory()
            argv = build_remote_inventory_argv(
                route.destination, policy, panes=panes, option_names=option_names, nonce=nonce
            )
            timed_out = False
            overflow_streams: frozenset[str] = frozenset()
            try:
                completed = self._run(
                    argv,
                    timeout=min(
                        remaining, policy.connect_timeout_seconds * policy.connection_attempts + 1
                    ),
                )
                stdout, stderr, returncode = (
                    completed.stdout or "",
                    completed.stderr or "",
                    completed.returncode,
                )
                timed_out = bool(getattr(completed, "timed_out", False))
                overflow_streams = frozenset(getattr(completed, "overflow_streams", frozenset()))
            except subprocess.TimeoutExpired as error:
                stdout = ""
                stderr = (
                    error.stderr.decode(errors="replace")
                    if isinstance(error.stderr, bytes)
                    else error.stderr or ""
                )
                returncode = None
                timed_out = True
            except OSError as error:
                stdout, stderr, returncode = "", clean_message(error), None
            observed_at = self._now_millis()
            # A capped stderr stream cannot establish exact-one-marker proof;
            # stdout overflow still permits a complete stderr marker proof.
            reached, remaining_stderr = (
                parse_reached_marker(stderr, nonce)
                if "stderr" not in overflow_streams
                else (False, stderr)
            )
            if reached:
                try:
                    self._adapter.report_route(
                        host_id=host.host_id,
                        route=route.destination,
                        status="reachable",
                        mesh_revision=mesh_revision,
                        observed_at=observed_at,
                        timeout_seconds=max(0.001, deadline - time.monotonic()),
                    )
                except MeshStaleError:
                    raise
                except ContractError as error:
                    return self._row(
                        host, "error", None, [], route.destination, error.code, error.message
                    )
                if overflow_streams:
                    return self._row(
                        host,
                        "error",
                        None,
                        [],
                        route.destination,
                        "operation_failed",
                        "remote tmux output exceeded the consumer limit",
                    )
                if returncode != 0:
                    return self._row(
                        host,
                        "error",
                        None,
                        [],
                        route.destination,
                        "operation_failed",
                        (
                            "remote tmux inventory timed out"
                            if timed_out
                            else "remote tmux inventory command failed"
                        ),
                    )
                try:
                    parsed = parse_remote_inventory(
                        stdout,
                        host_id=host.host_id,
                        panes_requested=panes,
                        option_names=option_names,
                    )
                except ContractError as error:
                    return self._row(
                        host, "error", None, [], route.destination, error.code, error.message
                    )
                if parsed.status == "tmux_missing":
                    return self._row(
                        host,
                        "tmux_missing",
                        None,
                        [],
                        route.destination,
                        "tmux_missing",
                        "tmux is not available",
                        native_hostname=parsed.native_hostname,
                    )
                if parsed.status == "tmux_error":
                    return self._row(
                        host,
                        "error",
                        None,
                        [],
                        route.destination,
                        "operation_failed",
                        parsed.error_message or "remote tmux inventory failed",
                        native_hostname=parsed.native_hostname,
                    )
                # Both a missing server and a live empty server are successful
                # authoritative inventories; generation captures the distinction.
                return self._row(
                    host,
                    "ok",
                    parsed.generation,
                    parsed.sessions,
                    route.destination,
                    None,
                    None,
                    native_hostname=parsed.native_hostname,
                )
            if _transport_failure(remaining_stderr, timed_out=timed_out):
                last_transport = clean_message(remaining_stderr or "SSH transport failed")
                try:
                    self._adapter.report_route(
                        host_id=host.host_id,
                        route=route.destination,
                        status="unreachable",
                        mesh_revision=mesh_revision,
                        observed_at=observed_at,
                        timeout_seconds=max(0.001, deadline - time.monotonic()),
                    )
                except MeshStaleError:
                    raise
                except ContractError as error:
                    return self._row(host, "error", None, [], None, error.code, error.message)
                continue
            saw_unclassified = True
            last_unclassified = clean_message(
                "remote SSH output exceeded the consumer limit"
                if overflow_streams
                else remaining_stderr or "remote SSH command did not prove a reached host"
            )
        if not saw_unclassified:
            return self._row(
                host, "unreachable", None, [], None, "host_unreachable", last_transport
            )
        return self._row(host, "error", None, [], None, "operation_failed", last_unclassified)

    def _row(
        self,
        host: MeshHost,
        status: str,
        generation: str | None,
        sessions: Sequence[Session],
        route: str | None,
        error_code: str | None,
        message: str | None,
        *,
        native_hostname: str | None = None,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "hostId": host.host_id,
            "display": host.display,
            "local": False,
            "status": status,
            "observedAt": self._now_millis(),
            "nativeHostname": native_hostname,
            "serverGeneration": generation,
            "route": route,
            "sessions": [session.as_dict() for session in sessions],
        }
        if error_code is not None and message is not None:
            row["error"] = {"code": error_code, "message": clean_message(message)}
        return row
