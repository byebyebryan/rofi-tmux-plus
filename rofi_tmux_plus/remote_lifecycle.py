"""Nonce-marked remote Tmux Session v1 lifecycle operations.

The remote side is a fixed POSIX shell program.  It receives request values as
positional arguments only; in particular no session name, path, option, or
command argument is ever spliced into shell source.
"""

from __future__ import annotations

import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .bounded_process import BoundedCompleted, run_bounded
from .config import Config, has_control, require_clean_text
from .errors import ContractError, clean_message
from .lifecycle import _validate_name, _validate_reference_inputs, local_mutation_lock
from .mesh_adapter import HostMeshAdapter, MeshHost, MeshPolicy
from .model import Session
from .remote_inventory import (
    _MAX_OUTPUT,
    _decode_field,
    _number,
    _transport_failure,
    generate_nonce,
    parse_reached_marker,
    parse_remote_inventory,
)
from .tmux import validate_session_id, validate_user_option

_REMOTE_TIMEOUT_SECONDS = 12.0
_MAX_ACTION_OUTPUT = _MAX_OUTPUT
_ERROR_CODES = {
    "invalid_cwd",
    "operation_failed",
    "session_exists",
    "session_not_found",
    "stale_session",
    "tmux_missing",
}


# This holder owns the first operation-token write.  A creator therefore never
# has an unmarked, fallible metadata window.  Its timeout cleanup rechecks the
# token before killing, and the parent transaction independently checks the
# full reference plus token before any rollback.
_HOLDER = r'''set -u
target="$(tmux display-message -p -t "$TMUX_PANE" '#{session_id}' 2>/dev/null || :)"
token=$1
timeout=$2
defer=$3
if [ -z "$target" ] || ! tmux set-option -q -t "$target" @rofi_tmux_plus_operation "$token"; then
  [ -n "$target" ] && tmux kill-session -t "$target" 2>/dev/null || :
  exit 1
fi
started="$(date +%s)"
while [ "$(tmux show-options -qv -t "$target" @rofi_tmux_plus_release 2>/dev/null || :)" != "$token" ]; do
  now="$(date +%s)"
  if [ $((now - started)) -ge "$timeout" ]; then
    marker="$(tmux show-options -qv -t "$target" @rofi_tmux_plus_operation 2>/dev/null || :)"
    [ "$marker" = "$token" ] && tmux kill-session -t "$target" 2>/dev/null || :
    exit 0
  fi
  sleep 0.05
done
tmux set-option -qu -t "$target" @rofi_tmux_plus_release 2>/dev/null || :
if [ "$defer" = 1 ]; then
  started="$(date +%s)"
  while ! tmux list-clients -t "$target" 2>/dev/null | grep -q .; do
    now="$(date +%s)"
    if [ $((now - started)) -ge "$timeout" ]; then
      marker="$(tmux show-options -qv -t "$target" @rofi_tmux_plus_pending 2>/dev/null || :)"
      [ "$marker" = "$token" ] && tmux kill-session -t "$target" 2>/dev/null || :
      exit 0
    fi
    sleep 0.10
  done
  tmux set-option -qu -t "$target" @rofi_tmux_plus_pending 2>/dev/null || :
fi
shift 3
if [ "$1" = __ROFI_TMUX_PLUS_DEFAULT_SHELL__ ]; then exec "${SHELL:-/bin/sh}"; fi
exec "$@"'''


# Output uses the inventory framing for H/G/D plus a small action record.  All
# error records are successful shell protocol records, so a domain failure is
# distinguishable from SSH transport failure after the reached marker.
_REMOTE_PROGRAM = rf"""set -u
hex() {{ LC_ALL=C od -An -v -tx1 | tr -d ' \n'; }}
field() {{ printf '\t'; "$@" | hex; }}
literal() {{ printf '\t'; printf '%s' "$1" | hex; }}
reply_error() {{ printf X; literal "$1"; literal "$2"; printf '\n'; exit 0; }}
native="$(hostname 2>/dev/null || uname -n 2>/dev/null || :)"
printf H; literal "$native"; printf '\n'
[ "$#" -ge 1 ] || reply_error operation_failed 'missing lifecycle action'
action=$1; shift
command -v tmux >/dev/null 2>&1 || reply_error tmux_missing 'tmux is not available'
emit_records=0
generation() {{
  socket="$(tmux display-message -p '#{{socket_path}}' 2>/dev/null)" || return 1
  started="$(tmux display-message -p '#{{start_time}}' 2>/dev/null)" || return 1
  pid="$(tmux display-message -p '#{{pid}}' 2>/dev/null)" || return 1
  [ -n "$socket" ] && [ -n "$started" ] && [ -n "$pid" ] || return 1
  current_generation="tmux-v1:$started:$pid:$socket"
  [ "$emit_records" = 1 ] && emit_generation_record
  return 0
}}
emit_generation_record() {{
  printf G; literal "$socket"; literal "$started"; literal "$pid"; printf '\n'
}}
descriptor() {{
  sid=$1
  created="$(tmux display-message -p -t "$sid" '#{{session_created}}' 2>/dev/null)" || return 1
  name="$(tmux display-message -p -t "$sid" '#{{session_name}}' 2>/dev/null)" || return 1
  activity="$(tmux display-message -p -t "$sid" '#{{session_activity}}' 2>/dev/null)" || return 1
  last="$(tmux display-message -p -t "$sid" '#{{session_last_attached}}' 2>/dev/null)" || return 1
  clients="$(tmux display-message -p -t "$sid" '#{{session_attached}}' 2>/dev/null)" || return 1
  windows="$(tmux display-message -p -t "$sid" '#{{session_windows}}' 2>/dev/null)" || return 1
  path="$(tmux display-message -p -t "$sid" '#{{session_path}}' 2>/dev/null)" || return 1
  window="$(tmux display-message -p -t "$sid" '#{{window_name}}' 2>/dev/null)" || return 1
  current_path="$(tmux display-message -p -t "$sid" '#{{pane_current_path}}' 2>/dev/null)" || return 1
  pending=0
  tmux show-options -q -t "$sid" | awk '$0 == "@rofi_tmux_plus_pending" || index($0,"@rofi_tmux_plus_pending ") == 1 {{ f=1 }} END {{ exit !f }}' && pending=1
  descriptor_sid=$sid
  descriptor_created=$created
  descriptor_name=$name
  descriptor_clients=$clients
  descriptor_activity=$activity
  descriptor_last=$last
  descriptor_windows=$windows
  descriptor_path=$path
  descriptor_window=$window
  descriptor_current_path=$current_path
  descriptor_pending=$pending
  [ "$emit_records" = 1 ] && emit_descriptor_record
  return 0
}}
emit_descriptor_record() {{
  printf D; literal "$descriptor_sid"; literal "$descriptor_created"; literal "$descriptor_name"; literal "$descriptor_activity"; literal "$descriptor_last"; literal "$descriptor_clients"; literal "$descriptor_windows"; literal "$descriptor_path"; literal "$descriptor_window"; literal "$descriptor_current_path"; literal "$descriptor_pending"; printf '\n'
}}
validate() {{
  expected_generation=$1; sid=$2; expected_created=$3; expected_name=$4
  generation || reply_error stale_session 'the selected tmux server changed; refresh and try again'
  [ "$current_generation" = "$expected_generation" ] || reply_error stale_session 'the selected tmux server changed; refresh and try again'
  descriptor "$sid" || reply_error session_not_found 'the selected tmux session no longer exists'
  [ "$descriptor_created" = "$expected_created" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
  [ -z "$expected_name" ] || [ "$descriptor_name" = "$expected_name" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
}}
rollback() {{
  rollback_generation=$1; rollback_sid=$2; rollback_created=$3; rollback_token=$4
  emit_records=0 generation || return 0
  [ "$current_generation" = "$rollback_generation" ] || return 0
  descriptor "$rollback_sid" >/dev/null 2>&1 || return 0
  [ "$descriptor_created" = "$rollback_created" ] || return 0
  marker="$(tmux show-options -qv -t "$rollback_sid" @rofi_tmux_plus_operation 2>/dev/null || :)"
  [ "$marker" = "$rollback_token" ] && tmux kill-session -t "$rollback_sid" 2>/dev/null || :
}}
case "$action" in
  open)
    [ "$#" -eq 5 ] || reply_error operation_failed 'invalid open request'
    [ "$4" = 0 ] || [ "$4" = 1 ] || reply_error operation_failed 'invalid open request'
    open_expected=''
    [ "$4" = 0 ] || open_expected=$5
    validate "$1" "$2" "$3" "$open_expected"
    emit_records=1
    generation || reply_error operation_failed 'tmux could not read the server identity'
    [ "$current_generation" = "$1" ] || reply_error stale_session 'the selected tmux server changed; refresh and try again'
    descriptor "$2" || reply_error session_not_found 'the selected tmux session no longer exists'
    [ "$descriptor_created" = "$3" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
    [ "$4" = 0 ] || [ "$descriptor_name" = "$5" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
    printf 'R\tOPEN\n'
    ;;
  rename)
    [ "$#" -eq 5 ] || reply_error operation_failed 'invalid rename request'
    validate "$1" "$2" "$3" "$4"
    if tmux has-session -t "=$5" 2>/dev/null; then reply_error session_exists 'a tmux session with that exact name already exists'; fi
    validate "$1" "$2" "$3" "$4"
    tmux rename-session -t "$2" "$5" >/dev/null 2>&1 || reply_error operation_failed 'tmux could not rename the session'
    emit_records=1
    generation || reply_error operation_failed 'tmux could not read the server identity'
    [ "$current_generation" = "$1" ] || reply_error stale_session 'the selected tmux server changed; refresh and try again'
    descriptor "$2" || reply_error operation_failed 'tmux could not read the renamed session'
    [ "$descriptor_created" = "$3" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
    [ "$descriptor_name" = "$5" ] || reply_error operation_failed 'tmux did not retain the new session name'
    printf 'R\tRENAME\n'
    ;;
  kill)
    [ "$#" -eq 4 ] || reply_error operation_failed 'invalid kill request'
    validate "$1" "$2" "$3" "$4"
    observed=$descriptor_clients
    validate "$1" "$2" "$3" "$4"
    emit_records=1
    generation || reply_error operation_failed 'tmux could not read the server identity'
    [ "$current_generation" = "$1" ] || reply_error stale_session 'the selected tmux server changed; refresh and try again'
    descriptor "$2" || reply_error session_not_found 'the selected tmux session no longer exists'
    [ "$descriptor_created" = "$3" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
    [ "$descriptor_name" = "$4" ] || reply_error stale_session 'the selected tmux session changed; refresh and try again'
    tmux kill-session -t "$2" >/dev/null 2>&1 || reply_error operation_failed 'tmux could not kill the session'
    printf 'R\tKILL\t%s\n' "$observed"
    ;;
  create)
    [ "$#" -ge 7 ] || reply_error operation_failed 'invalid create request'
    name=$1; cwd=$2; cwd_provided=$3; token=$4; defer=$5; timeout=$6; option_count=$7; shift 7
    [ "$cwd_provided" = 0 ] || [ "$cwd_provided" = 1 ] || reply_error operation_failed 'invalid create request'
    [ "$cwd_provided" = 1 ] || cwd="${{HOME:-/}}"
    [ -d "$cwd" ] || reply_error invalid_cwd 'cwd must name an existing directory'
    if tmux has-session -t "=$name" 2>/dev/null; then reply_error session_exists 'a tmux session with that exact name already exists'; fi
    [ "$option_count" -ge 0 ] 2>/dev/null || reply_error operation_failed 'invalid create request'
    remaining=$option_count
    option_pairs=''
    while [ "$remaining" -gt 0 ]; do
      [ "$#" -ge 2 ] || reply_error operation_failed 'invalid create request'
      option_pairs="${{option_pairs}}$1	$2
"
      shift 2
      remaining=$((remaining - 1))
    done
    [ "$#" -ge 1 ] || set -- __ROFI_TMUX_PLUS_DEFAULT_SHELL__
    result="$(tmux new-session -d -P -F '#{{session_id}} #{{session_created}}' -s "$name" -c "$cwd" /bin/sh -c {shlex.quote(_HOLDER)} rofi-tmux-plus-holder "$token" "$timeout" "$defer" "$@" 2>&1)" || {{
      case "$result" in *duplicate*|*exists*) reply_error session_exists 'a tmux session with that exact name already exists' ;; *) reply_error operation_failed 'tmux could not create the session' ;; esac
    }}
    set -- $result
    [ "$#" -eq 2 ] || reply_error operation_failed 'tmux did not return the new session identity'
    new_sid=$1; new_created=$2
    generation || reply_error operation_failed 'tmux did not return a server identity'
    new_generation=$current_generation
    armed=1
    waited=0
    while [ "$waited" -lt 40 ]; do
      marker="$(tmux show-options -qv -t "$new_sid" @rofi_tmux_plus_operation 2>/dev/null || :)"
      [ "$marker" = "$token" ] && break
      sleep 0.05; waited=$((waited + 1))
    done
    [ "$marker" = "$token" ] || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'holding wrapper did not install its operation token'; }}
    while IFS="$(printf '\t')" read -r option value; do
      [ -n "$option" ] || continue
      tmux set-option -q -t "$new_sid" "$option" "$value" >/dev/null 2>&1 || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'tmux could not set requested session metadata'; }}
    done <<EOF
$option_pairs
EOF
    [ "$defer" != 1 ] || tmux set-option -q -t "$new_sid" @rofi_tmux_plus_pending "$token" >/dev/null 2>&1 || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'tmux could not set pending state'; }}
    descriptor "$new_sid" || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'tmux could not describe the new session'; }}
    [ "$descriptor_created" = "$new_created" ] || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'the new tmux session changed'; }}
    [ "$descriptor_name" = "$name" ] || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'tmux did not retain the new session name'; }}
    tmux set-option -q -t "$new_sid" @rofi_tmux_plus_release "$token" >/dev/null 2>&1 || {{ rollback "$new_generation" "$new_sid" "$new_created" "$token"; reply_error operation_failed 'tmux could not release the new session'; }}
    armed=0
    tmux set-option -qu -t "$new_sid" @rofi_tmux_plus_operation >/dev/null 2>&1 || :
    # The release commits creation.  Never turn a fast-exiting, successfully
    # released session into an ambiguous error by rereading tmux here: the
    # complete generation and descriptor above are the authoritative result.
    emit_generation_record
    emit_descriptor_record
    printf 'R\tCREATE\n'
    ;;
  *) reply_error operation_failed 'unknown lifecycle action' ;;
esac"""


def _marker_wrapper() -> str:
    return r'''printf '\036ROFI_PLUS_REACHED_V1:%s\037\n' "$1" >&2
shift
exec "$@"'''


def build_remote_lifecycle_argv(
    route: str, policy: MeshPolicy, *, nonce: str, action: str, values: Sequence[str]
) -> list[str]:
    """Build the only remote command shape used by lifecycle actions."""
    if len(nonce) < 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid reached-host nonce")
    if action not in {"open", "create", "rename", "kill"}:
        raise ValueError("invalid lifecycle action")
    domain = ["sh", "-c", _REMOTE_PROGRAM, "rofi-tmux-plus-remote", action, *values]
    remote = " ".join(
        shlex.quote(value)
        for value in ("sh", "-c", _marker_wrapper(), "rofi-plus-reached", nonce, *domain)
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


@dataclass(frozen=True, slots=True)
class RemoteAction:
    kind: str
    session: Session | None
    observed_clients: int | None
    route: str
    native_hostname: str | None


def _parse_action(output: str, *, host_id: str, route: str) -> RemoteAction:
    records = output.splitlines()
    errors = [line for line in records if line.startswith("X\t")]
    if errors:
        # A domain error may use its declared code only when it is the exact
        # two-record response produced by the fixed remote program. Otherwise
        # an attacker or a broken shell could smuggle a stable-looking error
        # alongside malformed inventory data.
        if len(records) != 2 or len(errors) != 1 or not records[0].startswith("H\t"):
            raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
        header = records[0].split("\t")
        if len(header) != 2:
            raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
        _decode_field(header[1])
        parts = errors[0].split("\t")
        if len(parts) != 3:
            raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
        code = _decode_field(parts[1])
        message = clean_message(_decode_field(parts[2]))
        if code not in _ERROR_CODES:
            code = "operation_failed"
        raise ContractError(code, message or "remote tmux lifecycle failed", host_id)
    action_records = [line for line in records if line.startswith("R\t")]
    if len(action_records) != 1:
        raise ContractError(
            "operation_failed", "remote lifecycle output omitted its result", host_id
        )
    result = action_records[0].split("\t")
    if len(result) not in {2, 3} or result[1] not in {"OPEN", "CREATE", "RENAME", "KILL"}:
        raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
    observed: int | None = None
    if result[1] == "KILL":
        if len(result) != 3:
            raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
        observed = _number(result[2])
    elif len(result) != 2:
        raise ContractError("operation_failed", "remote lifecycle framing is invalid", host_id)
    inventory_lines = [line for line in records if not line.startswith("R\t")]
    parsed = parse_remote_inventory(
        "\n".join(inventory_lines) + "\n",
        host_id=host_id,
        panes_requested=False,
        option_names=(),
    )
    if parsed.status is not None or len(parsed.sessions) != 1:
        raise ContractError(
            "operation_failed", "remote lifecycle output omitted its session", host_id
        )
    return RemoteAction(result[1], parsed.sessions[0], observed, route, parsed.native_hostname)


class RemoteLifecycle:
    def __init__(
        self,
        adapter: HostMeshAdapter,
        config: Config,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str] | BoundedCompleted] | None = None,
        nonce_factory: Callable[[], str] = generate_nonce,
        now_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        focus: Callable[[Session, str | None], bool] | None = None,
        terminal_spawner: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._runner = runner
        self._nonce_factory = nonce_factory
        self._now_millis = now_millis
        self._focus = focus
        self._terminal_spawner = terminal_spawner

    def _run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str] | BoundedCompleted:
        if self._runner is None:
            return run_bounded(
                argv,
                timeout=timeout,
                stdout_limit=_MAX_ACTION_OUTPUT,
                stderr_limit=_MAX_ACTION_OUTPUT,
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

    def _action(
        self, host: MeshHost, policy: MeshPolicy, revision: str, action: str, values: Sequence[str]
    ) -> RemoteAction:
        deadline = time.monotonic() + _REMOTE_TIMEOUT_SECONDS
        last_transport = "no configured route completed"
        last_unclassified = "remote SSH command did not prove a reached host"
        saw_unclassified = False
        for route in host.routes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            nonce = self._nonce_factory()
            argv = build_remote_lifecycle_argv(
                route.destination, policy, nonce=nonce, action=action, values=values
            )
            timed_out = False
            overflow: frozenset[str] = frozenset()
            try:
                completed = self._run(
                    argv,
                    timeout=min(
                        remaining, policy.connect_timeout_seconds * policy.connection_attempts + 2
                    ),
                )
                stdout, stderr, returncode = (
                    completed.stdout or "",
                    completed.stderr or "",
                    completed.returncode,
                )
                timed_out = bool(getattr(completed, "timed_out", False))
                overflow = frozenset(getattr(completed, "overflow_streams", frozenset()))
            except subprocess.TimeoutExpired as error:
                stdout = ""
                stderr = (
                    error.stderr.decode(errors="replace")
                    if isinstance(error.stderr, bytes)
                    else error.stderr or ""
                )
                returncode, timed_out = None, True
            except OSError as error:
                stdout, stderr, returncode = "", clean_message(error), None
            reached, residual = (
                parse_reached_marker(stderr, nonce) if "stderr" not in overflow else (False, stderr)
            )
            observed = self._now_millis()
            if reached:
                self._adapter.report_route(
                    host_id=host.host_id,
                    route=route.destination,
                    status="reachable",
                    mesh_revision=revision,
                    observed_at=observed,
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
                if overflow:
                    raise ContractError(
                        "operation_failed",
                        "remote tmux output exceeded the consumer limit",
                        host.host_id,
                    )
                if returncode != 0:
                    raise ContractError(
                        "operation_failed", "remote tmux lifecycle command failed", host.host_id
                    )
                result = _parse_action(stdout, host_id=host.host_id, route=route.destination)
                if result.kind != action.upper():
                    raise ContractError(
                        "operation_failed",
                        "remote lifecycle action framing is invalid",
                        host.host_id,
                    )
                return result
            if _transport_failure(residual, timed_out=timed_out):
                last_transport = clean_message(residual or "SSH transport failed")
                self._adapter.report_route(
                    host_id=host.host_id,
                    route=route.destination,
                    status="unreachable",
                    mesh_revision=revision,
                    observed_at=observed,
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
            else:
                saw_unclassified = True
                last_unclassified = clean_message(residual or last_unclassified)
        if not saw_unclassified:
            raise ContractError("host_unreachable", last_transport, host.host_id)
        raise ContractError("operation_failed", last_unclassified, host.host_id)

    @staticmethod
    def _validate_create(
        name: str,
        cwd: str | None,
        options: Sequence[tuple[str, str]],
        command: Sequence[str],
        defer: bool,
        timeout: int,
    ) -> None:
        _validate_name(name)
        if cwd is not None:
            require_clean_text(cwd, "cwd")
        if any("\x00" in item or has_control(item) for item in command):
            raise ContractError(
                "invalid_input", "command arguments must not contain NUL or control characters"
            )
        if defer and not command:
            raise ContractError("invalid_input", "--defer-until-attached requires a command")
        if not 1 <= timeout <= 3600:
            raise ContractError(
                "invalid_input", "attach timeout must be an integer from 1 through 3600"
            )
        for option, value in options:
            validate_user_option(option)
            require_clean_text(value, f"value for {option}")

    def open(
        self,
        host: MeshHost,
        policy: MeshPolicy,
        revision: str,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None,
    ) -> dict[str, object]:
        _validate_reference_inputs(generation, session_id, created_at, expected_name)
        result = self._action(
            host,
            policy,
            revision,
            "open",
            [
                generation,
                session_id,
                str(created_at),
                "1" if expected_name is not None else "0",
                expected_name or "",
            ],
        )
        assert result.session is not None
        focused = bool(self._focus and self._focus(result.session, result.native_hostname))
        launched = False
        if not focused:
            self._launch(result.route, policy, result.session.reference.session_id)
            launched = True
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": revision,
            "session": result.session.as_dict(),
            "focused": focused,
            "terminalLaunched": launched,
        }

    def _launch(self, route: str, policy: MeshPolicy, session_id: str) -> None:
        # OpenSSH concatenates remote command argv into a shell command. Keep
        # the validated session ID quoted inside one command string so ``$0``
        # remains tmux's literal target rather than remote-shell expansion.
        validate_session_id(session_id)
        remote_command = " ".join(
            shlex.quote(value) for value in ("tmux", "-u", "attach-session", "-t", session_id)
        )
        attach = [policy.executable, "-t", route, remote_command]
        if self._terminal_spawner is not None:
            self._terminal_spawner(attach)
            return
        from .lifecycle import spawn_terminal_command

        spawn_terminal_command(self._config, attach)

    def create(
        self,
        host: MeshHost,
        policy: MeshPolicy,
        revision: str,
        name: str,
        cwd: str | None,
        options: Sequence[tuple[str, str]],
        command: Sequence[str],
        defer: bool,
        attach_timeout: int | None,
        open_after: bool,
    ) -> dict[str, object]:
        timeout = self._config.attach_timeout_seconds if attach_timeout is None else attach_timeout
        self._validate_create(name, cwd, options, command, defer, timeout)
        token = secrets.token_urlsafe(24)
        values = [
            name,
            cwd or "",
            "1" if cwd is not None else "0",
            token,
            "1" if defer else "0",
            str(timeout),
            str(len(options)),
        ]
        for option, value in options:
            values.extend((option, value))
        values.extend(command)
        with local_mutation_lock(host.host_id):
            result = self._action(host, policy, revision, "create", values)
        assert result.session is not None
        response: dict[str, object] = {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": revision,
            "session": result.session.as_dict(),
        }
        if open_after:
            focused = bool(self._focus and self._focus(result.session, result.native_hostname))
            if not focused:
                self._launch(result.route, policy, result.session.reference.session_id)
            response.update({"focused": focused, "terminalLaunched": not focused})
        return response

    def rename(
        self,
        host: MeshHost,
        policy: MeshPolicy,
        revision: str,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
        name: str,
    ) -> dict[str, object]:
        _validate_reference_inputs(generation, session_id, created_at, expected_name)
        _validate_name(name)
        with local_mutation_lock(host.host_id):
            result = self._action(
                host,
                policy,
                revision,
                "rename",
                [generation, session_id, str(created_at), expected_name, name],
            )
        assert result.session is not None
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": revision,
            "session": result.session.as_dict(),
        }

    def kill(
        self,
        host: MeshHost,
        policy: MeshPolicy,
        revision: str,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
    ) -> dict[str, object]:
        _validate_reference_inputs(generation, session_id, created_at, expected_name)
        with local_mutation_lock(host.host_id):
            result = self._action(
                host,
                policy,
                revision,
                "kill",
                [generation, session_id, str(created_at), expected_name],
            )
        assert result.session is not None and result.observed_clients is not None
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": revision,
            "reference": result.session.reference.as_dict(),
            "observedClients": result.observed_clients,
        }
