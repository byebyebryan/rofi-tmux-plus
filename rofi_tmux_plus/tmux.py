"""Default-server-only tmux process boundary and machine-readable inventory."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .errors import ContractError, NoServer, TmuxMissing, clean_message
from .model import Pane, Session, SessionReference

_SESSION_ID = re.compile(r"^\$[0-9]+$")
_PANE_ID = re.compile(r"^%[0-9]+$")
_USER_OPTION = re.compile(r"^@[A-Za-z0-9_.-]+$")
_MAX_SESSIONS = 256
_MAX_PANES = 512


def validate_user_option(name: str) -> str:
    if not _USER_OPTION.fullmatch(name):
        raise ContractError("invalid_input", f"invalid tmux user option: {name}")
    return name


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

    def session_ids(self) -> list[str]:
        output = self.run(["list-sessions", "-F", "#{session_id}"], no_server=True)
        if not output:
            return []
        ids = output.splitlines()
        if len(ids) > _MAX_SESSIONS or any(not _SESSION_ID.fullmatch(item) for item in ids):
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
        pending = self.option(session_id, "@rofi_tmux_plus_pending") is not None
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
        if len(ids) > _MAX_PANES or any(not _PANE_ID.fullmatch(item) for item in ids):
            raise ContractError("operation_failed", "tmux returned an invalid pane inventory")
        rows: list[Pane] = []
        for pane_id in ids:
            pid = _int_or_none(self.format(pane_id, "#{pane_pid}", no_server=True))
            current_path = self.format(pane_id, "#{pane_current_path}", no_server=True) or None
            command = self.format(pane_id, "#{pane_current_command}", no_server=True) or None
            rows.append(Pane(pane_id, pid, current_path, command))
        return rows

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
                self.descriptor(
                    host_id, generation, session_id, panes=panes, option_names=option_names
                )
                for session_id in ids
            ]
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
