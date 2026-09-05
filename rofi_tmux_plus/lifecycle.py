"""Local lifecycle operations with stable-reference and rollback safeguards."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .config import Config, has_control, require_clean_text
from .errors import ContractError, NoServer, clean_message
from .host import LocalHost, resolve_local_host
from .model import Session, SessionReference
from .tmux import TmuxClient, validate_session_id, validate_user_option

_OPERATION_OPTION = "@rofi_tmux_plus_operation"
_PENDING_OPTION = "@rofi_tmux_plus_pending"
_RELEASE_OPTION = "@rofi_tmux_plus_release"
_OPERATION_TOKEN_FAILURE = "holding wrapper did not install its operation token"


def now_millis() -> int:
    return time.time_ns() // 1_000_000


def _runtime_directory() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/rofi-tmux-plus-{os.getuid()}"))
    return base / "rofi-tmux-plus" / "locks"


@contextmanager
def local_mutation_lock(host_id: str) -> Iterator[None]:
    """Serialize only this tool's mutations; tmux remains the final authority."""
    directory = _runtime_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = directory / f"{host_id}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_clean(value: str) -> bool:
    return "\x00" not in value and not has_control(value)


def _validate_name(value: str) -> str:
    if not _is_clean(value):
        raise ContractError(
            "invalid_input", "session name must not contain NUL or control characters"
        )
    return value


def _validate_value(value: str, field: str) -> str:
    return require_clean_text(value, field)


def _validate_reference_inputs(
    generation: str,
    session_id: str,
    created_at: int,
    expected_name: str | None,
) -> None:
    if not generation or not _is_clean(generation):
        raise ContractError("invalid_input", "server generation must be nonempty and control-free")
    validate_session_id(session_id)
    if created_at < 0:
        raise ContractError("invalid_input", "created-at must be nonnegative")
    if expected_name is not None and not _is_clean(expected_name):
        raise ContractError(
            "invalid_input", "expected name must not contain NUL or control characters"
        )


def _wrapper_command(token: str, command: Sequence[str], *, defer: bool, timeout: int) -> list[str]:
    """Return argv for the private holding wrapper, never a user shell string."""
    if defer:
        body = "\n".join(
            (
                "set -eu",
                'target="$(tmux display-message -p -t "$TMUX_PANE" "#{session_id}")"',
                'if ! tmux set-option -q -t "$target" @rofi_tmux_plus_operation "$1"; then tmux kill-session -t "$target" 2>/dev/null || :; exit 1; fi',
                'setup_started="$(date +%s)"',
                'while [ "$(tmux show-options -qv -t "$target" @rofi_tmux_plus_release 2>/dev/null || :)" != "$1" ]; do',
                '  now="$(date +%s)"',
                '  if [ $((now - setup_started)) -ge "$2" ]; then',
                '    marker="$(tmux show-options -qv -t "$target" @rofi_tmux_plus_operation 2>/dev/null || :)"',
                '    if [ "$marker" = "$1" ]; then tmux kill-session -t "$target" 2>/dev/null || :; fi',
                "    exit 0",
                "  fi",
                "  sleep 0.05",
                "done",
                'tmux set-option -qu -t "$target" @rofi_tmux_plus_release 2>/dev/null || :',
                'started="$(date +%s)"',
                'while ! tmux list-clients -t "$target" 2>/dev/null | grep -q .; do',
                '  now="$(date +%s)"',
                '  if [ $((now - started)) -ge "$2" ]; then',
                '    pending="$(tmux show-options -qv -t "$target" @rofi_tmux_plus_pending 2>/dev/null || :)"',
                '    if [ "$pending" = "$1" ]; then tmux kill-session -t "$target" 2>/dev/null || :; fi',
                "    exit 0",
                "  fi",
                "  sleep 0.10",
                "done",
                'tmux set-option -qu -t "$target" @rofi_tmux_plus_pending 2>/dev/null || :',
                "shift 2",
                'exec "$@"',
            )
        )
        return ["/bin/sh", "-c", body, "rofi-tmux-plus-wrapper", token, str(timeout), *command]
    body = "\n".join(
        (
            "set -eu",
            'target="$(tmux display-message -p -t "$TMUX_PANE" "#{session_id}")"',
            'if ! tmux set-option -q -t "$target" @rofi_tmux_plus_operation "$1"; then tmux kill-session -t "$target" 2>/dev/null || :; exit 1; fi',
            'setup_started="$(date +%s)"',
            'while [ "$(tmux show-options -qv -t "$target" @rofi_tmux_plus_release 2>/dev/null || :)" != "$1" ]; do',
            '  now="$(date +%s)"',
            '  if [ $((now - setup_started)) -ge "$2" ]; then',
            '    marker="$(tmux show-options -qv -t "$target" @rofi_tmux_plus_operation 2>/dev/null || :)"',
            '    if [ "$marker" = "$1" ]; then tmux kill-session -t "$target" 2>/dev/null || :; fi',
            "    exit 0",
            "  fi",
            "  sleep 0.05",
            "done",
            'tmux set-option -qu -t "$target" @rofi_tmux_plus_release 2>/dev/null || :',
            "shift 2",
            'if [ "$1" = "__ROFI_TMUX_PLUS_DEFAULT_SHELL__" ]; then',
            '  exec "${SHELL:-/bin/sh}"',
            "fi",
            'exec "$@"',
        )
    )
    payload = list(command) if command else ["__ROFI_TMUX_PLUS_DEFAULT_SHELL__"]
    return ["/bin/sh", "-c", body, "rofi-tmux-plus-wrapper", token, str(timeout), *payload]


def focus_session_window(
    session_name: str | None,
    native_hostname: str | None,
    *,
    niri_command: Sequence[str] = ("niri",),
) -> bool:
    """Best-effort exact title focus shared by local and reached remote hosts."""
    if not os.environ.get("NIRI_SOCKET") or shutil.which(niri_command[0]) is None:
        return False
    try:
        reply = subprocess.run(
            [*niri_command, "msg", "-j", "windows"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
            check=False,
        )
        if reply.returncode:
            return False
        rows = json.loads(reply.stdout)
        if not isinstance(rows, list):
            return False
        name = (session_name or "").casefold()
        host = (native_hostname or "").split(".", 1)[0].casefold()
        if not name or not host:
            return False
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                continue
            title = str(row.get("title", "")).casefold()
            if title.startswith(f"{name}:") and title.rstrip().endswith(f"@ {host}"):
                focused = subprocess.run(
                    [*niri_command, "msg", "action", "focus-window", "--id", str(row["id"])],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
                return focused.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False
    return False


def spawn_terminal_command(config: Config, command: Sequence[str]) -> None:
    """Detach a terminal command in a collectable user scope when available."""
    terminal = [*config.terminal, "-e", *command]
    systemd_run = shutil.which("systemd-run")
    argv = (
        terminal
        if systemd_run is None
        else [
            systemd_run,
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            "--",
            *terminal,
        ]
    )
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


class LocalLifecycle:
    def __init__(
        self,
        tmux: TmuxClient,
        config: Config,
        *,
        host: LocalHost | None = None,
        niri_command: Sequence[str] = ("niri",),
        terminal_spawner: object | None = None,
    ) -> None:
        self.tmux = tmux
        self.config = config
        self.host = host
        self._niri_command = tuple(niri_command)
        self._terminal_spawner = terminal_spawner or self._spawn_terminal

    def resolve(self, host_id: str | None, mesh_revision: str | None) -> LocalHost:
        if mesh_revision is not None:
            raise ContractError(
                "stale_mesh", "the current local-only host mesh has no revision", host_id
            )
        return resolve_local_host(host_id, self.host)

    def inventory(
        self, host_id: str, *, panes: bool, option_names: Sequence[str]
    ) -> dict[str, object]:
        host = self.resolve(host_id, None)
        try:
            generation, sessions = self.tmux.inventory(
                host.host_id, panes=panes, option_names=option_names
            )
        except ContractError as error:
            if error.code == "tmux_missing":
                return self._host_row(host, "tmux_missing", None, [], error)
            return self._host_row(host, "error", None, [], error)
        return self._host_row(host, "ok", generation, sessions, None)

    @staticmethod
    def _host_row(
        host: LocalHost,
        status: str,
        generation: str | None,
        sessions: Sequence[Session],
        error: ContractError | None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "hostId": host.host_id,
            "display": host.display,
            "local": True,
            "status": status,
            "observedAt": now_millis(),
            "nativeHostname": host.native_hostname,
            "serverGeneration": generation,
            "route": None,
            "sessions": [session.as_dict() for session in sessions],
        }
        if error is not None:
            result["error"] = {"code": error.code, "message": error.message}
        return result

    def validate_reference(
        self,
        host_id: str,
        mesh_revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None = None,
    ) -> Session:
        _validate_reference_inputs(generation, session_id, created_at, expected_name)
        host = self.resolve(host_id, mesh_revision)
        session = self.tmux.find(SessionReference(host.host_id, generation, session_id, created_at))
        if expected_name is not None and session.name != expected_name:
            raise ContractError(
                "stale_session",
                "the selected tmux session changed; refresh and try again",
                host.host_id,
            )
        return session

    def open(
        self,
        host_id: str,
        mesh_revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None = None,
    ) -> dict[str, object]:
        session = self.validate_reference(
            host_id, mesh_revision, generation, session_id, created_at, expected_name
        )
        if self._focus_matching_window(session):
            return self._open_response(session, focused=True, terminal_launched=False)
        try:
            self._terminal_spawner(session.reference.session_id)
        except (OSError, subprocess.SubprocessError) as error:
            raise ContractError(
                "launch_failed", f"could not launch terminal: {clean_message(error)}", host_id
            ) from error
        return self._open_response(session, focused=False, terminal_launched=True)

    @staticmethod
    def _open_response(
        session: Session, *, focused: bool, terminal_launched: bool
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": None,
            "session": session.as_dict(),
            "focused": focused,
            "terminalLaunched": terminal_launched,
        }

    def _focus_matching_window(self, session: Session) -> bool:
        short_host = resolve_local_host(session.reference.host_id, self.host).host_id
        return focus_session_window(session.name, short_host, niri_command=self._niri_command)

    def _spawn_terminal(self, session_id: str) -> None:
        spawn_terminal_command(self.config, ["tmux", "-u", "attach-session", "-t", session_id])

    def create(
        self,
        host_id: str,
        mesh_revision: str | None,
        name: str,
        cwd: str | None,
        options: Sequence[tuple[str, str]],
        command: Sequence[str],
        defer_until_attached: bool,
        attach_timeout: int | None,
        open_after: bool,
    ) -> dict[str, object]:
        host = self.resolve(host_id, mesh_revision)
        _validate_name(name)
        if any(not _is_clean(item) for item in command):
            raise ContractError(
                "invalid_input",
                "command arguments must not contain NUL or control characters",
                host.host_id,
            )
        if defer_until_attached and not command:
            raise ContractError(
                "invalid_input", "--defer-until-attached requires a command", host.host_id
            )
        for option, value in options:
            validate_user_option(option)
            _validate_value(value, f"value for {option}")
        timeout = self.config.attach_timeout_seconds if attach_timeout is None else attach_timeout
        if not 1 <= timeout <= 3600:
            raise ContractError(
                "invalid_input",
                "attach timeout must be an integer from 1 through 3600",
                host.host_id,
            )
        selected_cwd = str(Path.home()) if cwd is None else cwd
        _validate_value(selected_cwd, "cwd")
        if not os.path.isdir(selected_cwd):
            raise ContractError("invalid_cwd", "cwd must name an existing directory", host.host_id)
        token = secrets.token_urlsafe(24)
        reference: SessionReference | None = None
        armed = False
        with local_mutation_lock(host.host_id):
            if self.tmux.has_name(name):
                raise ContractError(
                    "session_exists",
                    "a tmux session with that exact name already exists",
                    host.host_id,
                )
            try:
                session_id, created_at = self.tmux.create_detached(
                    name,
                    selected_cwd,
                    _wrapper_command(token, command, defer=defer_until_attached, timeout=timeout),
                )
            except ContractError as error:
                if error.code == "session_exists":
                    raise ContractError(
                        "session_exists",
                        "a tmux session with that exact name already exists",
                        host.host_id,
                    ) from error
                raise
            try:
                try:
                    generation = self.tmux.server_generation()
                except NoServer as error:
                    # The holding wrapper can fail and remove the just-created
                    # session before tmux returns control from create-detached.
                    # Normalize that ordering race at the lifecycle boundary;
                    # there is no stable reference to clean up in this case.
                    raise ContractError(
                        "operation_failed", _OPERATION_TOKEN_FAILURE, host.host_id
                    ) from error
                reference = SessionReference(host.host_id, generation, session_id, created_at)
                armed = True
                self._wait_for_operation_token(reference, token)
                for option, value in options:
                    self.tmux.set_option(session_id, option, value)
                if defer_until_attached:
                    self.tmux.set_option(session_id, _PENDING_OPTION, token)
                session = self.tmux.find(reference)
                self.tmux.set_option(session_id, _RELEASE_OPTION, token)
                # The wrapper may execute as soon as this succeeds.  From
                # here forward cleanup must never kill the session, even if
                # clearing bookkeeping reports an unexpected error.
                armed = False
                try:
                    self.tmux.unset_option(session_id, _OPERATION_OPTION)
                except ContractError:
                    # Release already committed a successful session. A
                    # bookkeeping cleanup failure cannot turn it into an
                    # ambiguous lifecycle result or justify killing it.
                    pass
            except ContractError:
                self._rollback(reference, token, armed)
                raise
        response: dict[str, object] = {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": None,
            "session": session.as_dict(),
        }
        if open_after:
            opened = self.open(
                host.host_id,
                None,
                session.reference.server_generation,
                session.reference.session_id,
                session.reference.created_at,
            )
            response["focused"] = opened["focused"]
            response["terminalLaunched"] = opened["terminalLaunched"]
        return response

    def _wait_for_operation_token(self, reference: SessionReference, token: str) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if self.tmux.option(reference.session_id, _OPERATION_OPTION) == token:
                    return
            except ContractError:
                break
            time.sleep(0.025)
        raise ContractError(
            "operation_failed",
            _OPERATION_TOKEN_FAILURE,
            reference.host_id,
        )

    def _rollback(self, reference: SessionReference | None, token: str, armed: bool) -> None:
        if not armed or reference is None:
            return
        try:
            current = self.tmux.find(reference)
            marker = self.tmux.option(reference.session_id, _OPERATION_OPTION)
            if current.reference == reference and marker == token:
                self.tmux.kill(reference.session_id)
        except ContractError:
            return

    def rename(
        self,
        host_id: str,
        mesh_revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
        name: str,
    ) -> dict[str, object]:
        _validate_name(name)
        host = self.resolve(host_id, mesh_revision)
        with local_mutation_lock(host.host_id):
            self.validate_reference(
                host.host_id, None, generation, session_id, created_at, expected_name
            )
            if self.tmux.has_name(name, except_session_id=session_id):
                raise ContractError(
                    "session_exists",
                    "a tmux session with that exact name already exists",
                    host.host_id,
                )
            self.validate_reference(
                host.host_id, None, generation, session_id, created_at, expected_name
            )
            self.tmux.rename(session_id, name)
            changed = self.validate_reference(
                host.host_id, None, generation, session_id, created_at, name
            )
        return {"schemaVersion": 1, "ok": True, "meshRevision": None, "session": changed.as_dict()}

    def kill(
        self,
        host_id: str,
        mesh_revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
    ) -> dict[str, object]:
        host = self.resolve(host_id, mesh_revision)
        with local_mutation_lock(host.host_id):
            session = self.validate_reference(
                host.host_id, None, generation, session_id, created_at, expected_name
            )
            observed_clients = session.attached_clients
            self.validate_reference(
                host.host_id, None, generation, session_id, created_at, expected_name
            )
            self.tmux.kill(session_id)
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": None,
            "reference": session.reference.as_dict(),
            "observedClients": observed_clients,
        }
