"""Default-server-only tmux process boundary and machine-readable inventory."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .config import require_clean_text
from .errors import ContractError, NoServer, TmuxMissing, clean_message
from .model import Pane, Session, SessionReference
from .tmux_wire import TmuxWireError, decode_tmux_argument, parse_explicit_user_options

_SESSION_ID = re.compile(r"^\$[0-9]+$")
_PANE_ID = re.compile(r"^%[0-9]+$")
_USER_OPTION = re.compile(r"^@[A-Za-z0-9_.-]+$")
_MAX_SESSIONS = 256
_MAX_PANES = 512
_PENDING_OPTION = "@rofi_tmux_plus_pending"
_FAST_GENERATION_FORMAT = "#{q/a:socket_path}\t#{q/a:start_time}\t#{q/a:pid}"
_FAST_SESSION_FORMAT = (
    "#{q/a:session_id}\t#{q/a:session_created}\t#{q/a:session_name}\t"
    "#{q/a:session_activity}\t#{q/a:session_last_attached}\t#{q/a:session_attached}\t"
    "#{q/a:session_windows}\t#{q/a:session_path}\t#{q/a:window_name}\t"
    "#{q/a:pane_current_path}"
)
_FAST_PANE_FORMAT = (
    "#{q/a:session_id}\t#{q/a:pane_id}\t#{q/a:pane_pid}\t"
    "#{q/a:pane_current_path}\t#{q/a:pane_current_command}"
)


class _FastPathUnavailable(Exception):
    """q/a output cannot safely replace the established per-field path."""


def validate_user_option(name: str) -> str:
    if not _USER_OPTION.fullmatch(name):
        raise ContractError("invalid_input", f"invalid tmux user option: {name}")
    return name


def validate_required_options(
    options: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Validate exact user-option preconditions without exposing their values.

    A repeated identical requirement is redundant and is collapsed.  A caller
    cannot require two different values for one option: such a request could
    never succeed and is rejected as invalid input before any lifecycle work.
    """
    result: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for name, value in options:
        validate_user_option(name)
        require_clean_text(value, f"value for {name}")
        if name in seen:
            if seen[name] != value:
                raise ContractError("invalid_input", f"conflicting required values for {name}")
            continue
        seen[name] = value
        result.append((name, value))
    return tuple(result)


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.fullmatch(session_id):
        raise ContractError("invalid_input", "session id must use tmux's $digits form")
    return session_id


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


class TmuxClient:
    """A narrowly scoped client; public callers can never select a socket.

    Tests may supply an explicit executable prefix (``tmux -L unique``), but
    the public CLI constructs this class with the default server only.
    """

    def __init__(
        self,
        executable: Sequence[str] = ("tmux",),
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not executable:
            raise ValueError("tmux executable must not be empty")
        self._executable = tuple(executable)
        self._timeout_seconds = timeout_seconds
        self._deadline: float | None = None

    def _run(self, args: Sequence[str], *, timeout: float | None = None) -> Completed:
        argv = [*self._executable, *args]
        selected_timeout = self._timeout_seconds if timeout is None else timeout
        if self._deadline is not None:
            selected_timeout = min(selected_timeout, self._deadline - time.monotonic())
            if selected_timeout <= 0:
                raise ContractError(
                    "operation_failed", "tmux inventory exceeded the local deadline"
                )
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=selected_timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise TmuxMissing() from error
        except subprocess.TimeoutExpired as error:
            raise ContractError(
                "operation_failed", "tmux did not respond before the local deadline"
            ) from error
        except OSError as error:
            raise ContractError(
                "operation_failed", f"could not execute tmux: {clean_message(error)}"
            ) from error
        return Completed(result.returncode, result.stdout, result.stderr)

    @staticmethod
    def _without_final_newline(value: str) -> str:
        return value.removesuffix("\n")

    @staticmethod
    def _no_server(result: Completed) -> bool:
        text = f"{result.stdout}\n{result.stderr}".casefold()
        return (
            "no server running" in text
            or "failed to connect to server" in text
            or "error connecting to" in text
        )

    def run(self, args: Sequence[str], *, no_server: bool = False) -> str:
        result = self._run(args)
        if result.returncode == 0:
            return self._without_final_newline(result.stdout)
        if no_server and self._no_server(result):
            raise NoServer()
        raise ContractError("operation_failed", clean_message(result.stderr or result.stdout))

    def try_run(self, args: Sequence[str]) -> Completed:
        return self._run(args)

    def format(self, target: str | None, template: str, *, no_server: bool = False) -> str:
        args = ["display-message", "-p"]
        if target is not None:
            args.extend(["-t", target])
        args.append(template)
        return self.run(args, no_server=no_server)

    def server_generation(self) -> str:
        # One format per process means a newline, tab, or delimiter in a path
        # can never corrupt the result.  The socket is opaque in the contract.
        socket_path = self.format(None, "#{socket_path}", no_server=True)
        started = self.format(None, "#{start_time}", no_server=True)
        pid = self.format(None, "#{pid}", no_server=True)
        if not socket_path or not started.isdecimal() or not pid.isdecimal():
            raise ContractError("operation_failed", "tmux returned an incomplete server identity")
        return f"tmux-v1:{started}:{pid}:{socket_path}"

    @staticmethod
    def _fast_rows(output: str, *, fields: int, what: str) -> list[tuple[str, ...]]:
        if not output:
            return []
        rows: list[tuple[str, ...]] = []
        try:
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) != fields:
                    raise TmuxWireError(f"tmux {what} record has the wrong field count")
                rows.append(tuple(decode_tmux_argument(part) for part in parts))
        except TmuxWireError as error:
            raise _FastPathUnavailable() from error
        return rows

    def _fast_generation(self) -> str:
        rows = self._fast_rows(
            self.run(["display-message", "-p", _FAST_GENERATION_FORMAT], no_server=True),
            fields=3,
            what="server identity",
        )
        if len(rows) != 1:
            raise _FastPathUnavailable()
        socket_path, started, pid = rows[0]
        if not socket_path or not started.isdecimal() or not pid.isdecimal():
            raise _FastPathUnavailable()
        return f"tmux-v1:{started}:{pid}:{socket_path}"

    def session_ids(self) -> list[str]:
        output = self.run(["list-sessions", "-F", "#{session_id}"], no_server=True)
        if not output:
            return []
        ids = output.splitlines()
        if (
            len(ids) > _MAX_SESSIONS
            or len(ids) != len(set(ids))
            or any(not _SESSION_ID.fullmatch(item) for item in ids)
        ):
            raise ContractError("operation_failed", "tmux returned an invalid session inventory")
        return ids

    def option(self, session_id: str, name: str) -> str | None:
        validate_user_option(name)
        present = self.run(["show-options", "-q", "-t", session_id], no_server=True)
        if not any(line == name or line.startswith(f"{name} ") for line in present.splitlines()):
            return None
        result = self.try_run(["show-options", "-qv", "-t", session_id, name])
        if result.returncode == 0:
            return self._without_final_newline(result.stdout)
        if self._no_server(result):
            raise NoServer()
        raise ContractError("operation_failed", clean_message(result.stderr or result.stdout))

    def descriptor(
        self,
        host_id: str,
        generation: str,
        session_id: str,
        *,
        panes: bool = False,
        option_names: Iterable[str] = (),
    ) -> Session:
        if not _SESSION_ID.fullmatch(session_id):
            raise ContractError("operation_failed", "tmux returned an invalid session id")
        # Dynamic fields are read independently.  tmux permits unusual external
        # names and paths; this avoids a separator-based protocol entirely.
        created = _int_or_none(self.format(session_id, "#{session_created}", no_server=True))
        if created is None:
            raise ContractError(
                "operation_failed", "tmux returned an invalid session creation time"
            )
        name = self.format(session_id, "#{session_name}", no_server=True)
        activity = _int_or_none(self.format(session_id, "#{session_activity}", no_server=True))
        last_attached = _int_or_none(
            self.format(session_id, "#{session_last_attached}", no_server=True)
        )
        attached = _int_or_none(self.format(session_id, "#{session_attached}", no_server=True))
        windows = _int_or_none(self.format(session_id, "#{session_windows}", no_server=True))
        path = self.format(session_id, "#{session_path}", no_server=True)
        current_window = self.format(session_id, "#{window_name}", no_server=True)
        current_path = self.format(session_id, "#{pane_current_path}", no_server=True)
        pending = self.option(session_id, _PENDING_OPTION) is not None
        selected_options = {name: self.option(session_id, name) for name in option_names}
        pane_rows = self.panes(session_id) if panes else None
        return Session(
            SessionReference(host_id, generation, session_id, created),
            name or None,
            activity,
            last_attached,
            attached,
            pending,
            windows,
            path or None,
            current_window or None,
            current_path or None,
            tuple(pane_rows) if pane_rows is not None else None,
            selected_options if option_names else None,
        )

    def panes(self, session_id: str) -> list[Pane]:
        output = self.run(
            ["list-panes", "-s", "-t", session_id, "-F", "#{pane_id}"], no_server=True
        )
        if not output:
            return []
        ids = output.splitlines()
        if (
            len(ids) > _MAX_PANES
            or len(ids) != len(set(ids))
            or any(not _PANE_ID.fullmatch(item) for item in ids)
        ):
            raise ContractError("operation_failed", "tmux returned an invalid pane inventory")
        rows: list[Pane] = []
        for pane_id in ids:
            pid = _int_or_none(self.format(pane_id, "#{pane_pid}", no_server=True))
            current_path = self.format(pane_id, "#{pane_current_path}", no_server=True) or None
            command = self.format(pane_id, "#{pane_current_command}", no_server=True) or None
            rows.append(Pane(pane_id, pid, current_path, command))
        return rows

    def _inventory_fast(
        self,
        host_id: str,
        *,
        panes: bool,
        option_names: tuple[str, ...],
    ) -> tuple[str | None, list[Session]]:
        try:
            generation = self._fast_generation()
        except NoServer:
            return None, []
        try:
            session_rows = self._fast_rows(
                self.run(["list-sessions", "-F", _FAST_SESSION_FORMAT], no_server=True),
                fields=10,
                what="session",
            )
        except NoServer:
            # Keep the live-empty distinction when a server exits between the
            # identity probe and the list command, exactly as the legacy path.
            return generation, []
        if len(session_rows) > _MAX_SESSIONS:
            raise _FastPathUnavailable()
        records: dict[str, tuple[str, ...]] = {}
        for row in session_rows:
            session_id, created, *_rest = row
            if (
                not _SESSION_ID.fullmatch(session_id)
                or session_id in records
                or _int_or_none(created) is None
            ):
                raise _FastPathUnavailable()
            records[session_id] = row

        selected_options: dict[str, dict[str, str | None]] = {}
        pending: dict[str, bool] = {}
        for session_id in records:
            try:
                snapshot = self.run(["show-options", "-q", "-t", session_id], no_server=True)
                found_pending, selected = parse_explicit_user_options(
                    snapshot,
                    option_names,
                    pending_name=_PENDING_OPTION,
                )
            except TmuxWireError as error:
                raise _FastPathUnavailable() from error
            pending[session_id] = found_pending
            selected_options[session_id] = selected

        pane_rows: dict[str, list[Pane]] = {session_id: [] for session_id in records}
        if panes and records:
            raw_panes = self._fast_rows(
                self.run(["list-panes", "-a", "-F", _FAST_PANE_FORMAT], no_server=True),
                fields=5,
                what="pane",
            )
            seen_panes: set[str] = set()
            for pane_count, (session_id, pane_id, pid, current_path, command) in enumerate(
                raw_panes
            ):
                if (
                    session_id not in pane_rows
                    or not _PANE_ID.fullmatch(pane_id)
                    or pane_id in seen_panes
                    or pane_count >= _MAX_PANES
                ):
                    raise _FastPathUnavailable()
                seen_panes.add(pane_id)
                pane_rows[session_id].append(
                    Pane(pane_id, _int_or_none(pid), current_path or None, command or None)
                )

        sessions: list[Session] = []
        for session_id, row in records.items():
            (
                _session_id,
                created,
                name,
                activity,
                last_attached,
                attached,
                windows,
                path,
                current_window,
                current_path,
            ) = row
            created_at = _int_or_none(created)
            assert created_at is not None
            sessions.append(
                Session(
                    SessionReference(host_id, generation, session_id, created_at),
                    name or None,
                    _int_or_none(activity),
                    _int_or_none(last_attached),
                    _int_or_none(attached),
                    pending[session_id],
                    _int_or_none(windows),
                    path or None,
                    current_window or None,
                    current_path or None,
                    tuple(pane_rows[session_id]) if panes else None,
                    selected_options[session_id] if option_names else None,
                )
            )
        return generation, sessions

    def _inventory_legacy(
        self,
        host_id: str,
        *,
        panes: bool,
        option_names: tuple[str, ...],
    ) -> tuple[str | None, list[Session]]:
        try:
            generation = self.server_generation()
        except NoServer:
            return None, []
        try:
            ids = self.session_ids()
        except NoServer:
            # ``exit-empty off`` can keep a real server alive without a
            # session. Preserve that observable distinction.
            return generation, []
        return generation, [
            self.descriptor(host_id, generation, session_id, panes=panes, option_names=option_names)
            for session_id in ids
        ]

    def inventory(
        self,
        host_id: str,
        *,
        panes: bool = False,
        option_names: Iterable[str] = (),
    ) -> tuple[str | None, list[Session]]:
        previous_deadline = self._deadline
        self._deadline = time.monotonic() + self._timeout_seconds
        try:
            selected_option_names = tuple(option_names)
            for name in selected_option_names:
                validate_user_option(name)
            try:
                return self._inventory_fast(
                    host_id, panes=panes, option_names=selected_option_names
                )
            except _FastPathUnavailable:
                return self._inventory_legacy(
                    host_id, panes=panes, option_names=selected_option_names
                )
        finally:
            self._deadline = previous_deadline

    def find(self, reference: SessionReference) -> Session:
        try:
            current_generation = self.server_generation()
        except NoServer as error:
            raise ContractError(
                "stale_session", "the selected tmux server is no longer running", reference.host_id
            ) from error
        if current_generation != reference.server_generation:
            raise ContractError(
                "stale_session",
                "the selected tmux server changed; refresh and try again",
                reference.host_id,
            )
        try:
            known = self.session_ids()
        except NoServer as error:
            raise ContractError(
                "stale_session",
                "the selected tmux server changed; refresh and try again",
                reference.host_id,
            ) from error
        if reference.session_id not in known:
            raise ContractError(
                "session_not_found", "the selected tmux session no longer exists", reference.host_id
            )
        descriptor = self.descriptor(reference.host_id, current_generation, reference.session_id)
        if descriptor.reference.created_at != reference.created_at:
            raise ContractError(
                "stale_session",
                "the selected tmux session changed; refresh and try again",
                reference.host_id,
            )
        return descriptor

    def create_detached(self, name: str, cwd: str, command: Sequence[str]) -> tuple[str, int]:
        result = self._run(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{session_id} #{session_created}",
                "-s",
                name,
                "-c",
                cwd,
                *command,
            ]
        )
        if result.returncode != 0:
            diagnostic = clean_message(result.stderr or result.stdout)
            if (
                "duplicate session" in diagnostic.casefold()
                or "already exists" in diagnostic.casefold()
            ):
                raise ContractError(
                    "session_exists", "a tmux session with that exact name already exists"
                )
            raise ContractError("operation_failed", diagnostic)
        fields = self._without_final_newline(result.stdout).split(" ")
        if len(fields) != 2 or not _SESSION_ID.fullmatch(fields[0]) or not fields[1].isdecimal():
            raise ContractError("operation_failed", "tmux did not return the new session identity")
        return fields[0], int(fields[1])

    def set_option(self, session_id: str, name: str, value: str) -> None:
        validate_user_option(name)
        self.run(["set-option", "-q", "-t", session_id, name, value])

    def unset_option(self, session_id: str, name: str) -> None:
        validate_user_option(name)
        result = self.try_run(["set-option", "-qu", "-t", session_id, name])
        if result.returncode and not self._no_server(result):
            raise ContractError("operation_failed", clean_message(result.stderr or result.stdout))

    def rename(self, session_id: str, name: str) -> None:
        self.run(["rename-session", "-t", session_id, name])

    def kill(self, session_id: str) -> None:
        self.run(["kill-session", "-t", session_id])

    def has_name(self, name: str, *, except_session_id: str | None = None) -> bool:
        try:
            ids = self.session_ids()
        except NoServer:
            return False
        for session_id in ids:
            if (
                session_id != except_session_id
                and self.format(session_id, "#{session_name}", no_server=True) == name
            ):
                return True
        return False
