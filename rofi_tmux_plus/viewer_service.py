"""Bounded Niri/Kitty identity and exact attachment-close helpers."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ContractError
from .model import Session

_METADATA_ENV = "ROFI_TMUX_PLUS_VIEWER_V1"
_MAX_NIRI_BYTES = 1024 * 1024
_MAX_PROC_FILE = 256 * 1024
_MAX_PROCESSES = 256
_MAX_DEPTH = 12
_VIEWER_ID_PREFIX = "tv1_"


def kitty_configured(config: Config) -> bool:
    """Return true only when the configured terminal executable is Kitty itself."""
    return bool(config.terminal) and Path(config.terminal[0]).name.casefold() == "kitty"


def launch_environment(reference: Mapping[str, object]) -> dict[str, str]:
    """Create immutable process-environment metadata for one Kitty attachment."""
    _launch_id, environment = launch_metadata(reference)
    return environment


def launch_metadata(reference: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    """Return one launch ID and the environment carrying that exact ID."""
    launch_id = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    metadata = {
        "schemaVersion": 1,
        "launchId": launch_id,
        "sessionRef": dict(reference),
    }
    return launch_id, {_METADATA_ENV: json.dumps(metadata, separators=(",", ":"), sort_keys=True)}


@dataclass(frozen=True, slots=True)
class Viewer:
    viewer_id: str
    window_id: int
    window_pid: int
    window_start: int
    attachment_pid: int
    attachment_start: int
    launch_id: str | None
    session_ref: Mapping[str, object]
    attachment_argv: tuple[str, ...]
    tty_nr: int

    def public_dict(self) -> dict[str, object]:
        return {"viewerId": self.viewer_id, "windowId": self.window_id}


@dataclass(frozen=True, slots=True)
class ViewerInspection:
    status: str
    viewers: tuple[Viewer, ...]
    close_safe: bool
    reason: str | None = None
    pending_launch_ids: tuple[str, ...] = ()

    def as_fields(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "viewers": [viewer.public_dict() for viewer in self.viewers],
            "closeSafe": self.close_safe,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True, slots=True)
class _Proc:
    pid: int
    ppid: int
    pgrp: int
    session: int
    tty_nr: int
    start: int
    argv: tuple[str, ...]


def effective_destroy_unattached(tmux: object, session_id: str) -> str | None:
    """Read the explicit session override, then server/global value.

    An unreadable or absent value remains unknown. Callers must not treat an
    unknown value as safe because tmux options can be inherited.
    """
    snapshot = tmux.run(["show-options", "-q", "-t", session_id], no_server=True)  # type: ignore[attr-defined]
    if any(
        line == "destroy-unattached" or line.startswith("destroy-unattached ")
        for line in snapshot.splitlines()
    ):
        result = tmux.try_run(["show-options", "-qv", "-t", session_id, "destroy-unattached"])  # type: ignore[attr-defined]
    else:
        result = tmux.try_run(["show-options", "-gqv", "destroy-unattached"])  # type: ignore[attr-defined]
    if result.returncode != 0:
        return None
    return result.stdout.removesuffix("\n").strip().casefold()


def inspect_viewers(
    session: Session,
    config: Config,
    *,
    local_tmux: object | None = None,
    remote_route: str | None = None,
    remote_executable: str | None = None,
    remote_native_hostname: str | None = None,
    destroy_unattached: str | None,
    niri_command: Sequence[str] = ("niri",),
) -> ViewerInspection:
    close_safe = destroy_unattached == "off"
    if not kitty_configured(config):
        return ViewerInspection(
            "unsupported", (), close_safe, "unsupported terminal; Kitty viewer proof unavailable"
        )
    windows = _niri_windows(niri_command)
    if windows is None:
        return ViewerInspection(
            "unsupported",
            (),
            close_safe,
            "unsupported compositor; Niri window inventory unavailable",
        )

    local_clients: set[int] | None = None
    if remote_route is None and local_tmux is not None:
        try:
            local_clients = set(local_tmux.client_pids(session.reference.session_id))  # type: ignore[attr-defined]
        except (ContractError, OSError, ValueError):
            local_clients = None

    matches: list[Viewer] = []
    unverified = False
    ambiguous = False
    unsupported_candidate = False
    title_candidate = False
    pending_launch_ids: set[str] = set()
    for row in windows:
        if not isinstance(row, dict):
            continue
        app_id = row.get("app_id")
        if not isinstance(app_id, str):
            continue
        window_id = row.get("id")
        window_pid = row.get("pid")
        if (
            type(window_id) is not int
            or window_id <= 0
            or type(window_pid) is not int
            or window_pid <= 0
        ):
            continue
        title_match = _title_matches(row.get("title"), session, remote_native_hostname)
        title_candidate = title_candidate or title_match
        if app_id != "kitty":
            related = False
            if remote_route is None and local_clients is not None:
                direct_children = _direct_children(window_pid)
                related = any(item.pid in local_clients for item in direct_children)
                if not related:
                    tree, complete = _process_tree(window_pid)
                    related = complete and any(item.pid in local_clients for item in tree)
                if related:
                    unsupported_candidate = True
                elif title_match:
                    unverified = True
            elif remote_route is not None:
                expected = _remote_attach_argv(
                    remote_executable or "ssh", remote_route, session.reference.session_id
                )
                direct_children = _direct_children(window_pid)
                related = any(item.argv == expected for item in direct_children)
                if not related:
                    tree, complete = _process_tree(window_pid)
                    related = complete and any(item.argv == expected for item in tree)
                if related or title_match:
                    unverified = True
            elif title_match:
                unverified = True
            continue
        proc = _proc(window_pid)
        if proc is None:
            if title_match:
                unverified = True
            continue
        metadata, marker_present = _read_metadata(window_pid)
        marked = metadata is not None
        if marked and metadata.get("sessionRef") != session.reference.as_dict():
            continue

        launch_id: str | None = None
        if marked:
            candidate_launch_id = metadata.get("launchId")
            if (
                not isinstance(candidate_launch_id, str)
                or not candidate_launch_id
                or len(candidate_launch_id) > 128
            ):
                unverified = True
                continue
            launch_id = candidate_launch_id

        direct_children = _direct_children(window_pid)
        if remote_route is None:
            direct_attachments = [
                item
                for item in direct_children
                if item.tty_nr != 0 and _is_tmux_attach(item.argv, session.reference.session_id)
            ]
            related_direct = [
                item
                for item in direct_attachments
                if local_clients is not None and item.pid in local_clients
            ]
            # An unrelated Kitty layout cannot make this session ambiguous.
            # For legacy adoption, first prove that one of its exact tmux client
            # PIDs is attached to the requested session.
            if not marked and not related_direct:
                if direct_attachments or title_match:
                    unverified = True
                continue
        else:
            expected = _remote_attach_argv(
                remote_executable or "ssh", remote_route, session.reference.session_id
            )
            related_direct = [
                item for item in direct_children if item.tty_nr != 0 and item.argv == expected
            ]
            if not marked:
                if title_match or related_direct:
                    unverified = True
                continue

        tree, complete = _process_tree(window_pid)
        if not complete:
            if marked and launch_id is not None:
                if remote_route is None and direct_attachments:
                    unverified = True
                else:
                    pending_launch_ids.add(launch_id)
            elif title_match or related_direct:
                unverified = True
            continue
        direct = [item for item in tree if item.ppid == window_pid and item.tty_nr != 0]
        tty_groups = {item.tty_nr for item in tree if item.tty_nr != 0}
        if len(tty_groups) > 1:
            ambiguous = True
            continue

        if remote_route is None:
            candidates = [
                item
                for item in direct
                if item.pid in (local_clients or set())
                and _is_tmux_attach(item.argv, session.reference.session_id)
            ]
        else:
            candidates = [item for item in direct if item.argv == expected]
        if len(candidates) != 1:
            if len(candidates) > 1:
                ambiguous = True
            elif marked and launch_id is not None:
                matching_attachment = (
                    any(_is_tmux_attach(item.argv, session.reference.session_id) for item in direct)
                    if remote_route is None
                    else any(item.argv == expected for item in direct)
                )
                if matching_attachment:
                    unverified = True
                else:
                    pending_launch_ids.add(launch_id)
            elif title_match or related_direct or marker_present:
                unverified = True
            continue

        attachment = candidates[0]
        if marker_present and not marked:
            unverified = True
            continue
        viewer_id = _viewer_id(
            window_id,
            window_pid,
            proc.start,
            attachment.pid,
            attachment.start,
            launch_id,
            session.reference.as_dict(),
        )
        matches.append(
            Viewer(
                viewer_id,
                window_id,
                window_pid,
                proc.start,
                attachment.pid,
                attachment.start,
                launch_id,
                session.reference.as_dict(),
                attachment.argv,
                attachment.tty_nr,
            )
        )

    # One Kitty process backing multiple Niri windows is not a frozen
    # one-window/one-process identity, even if each row reports the same PID.
    if len({row.window_pid for row in matches}) != len(matches):
        ambiguous = True
    if ambiguous or (unverified and matches):
        return ViewerInspection(
            "ambiguous",
            (),
            close_safe,
            "ambiguous Kitty window or PTY layout",
            tuple(sorted(pending_launch_ids)),
        )
    if unverified:
        return ViewerInspection(
            "unverified",
            (),
            close_safe,
            "matching Kitty window identity is unverified",
            tuple(sorted(pending_launch_ids)),
        )
    if unsupported_candidate:
        return ViewerInspection(
            "unsupported", (), close_safe, "matching attachment is in an unsupported terminal"
        )
    if matches:
        reason = None if close_safe else "destroy-unattached is not provably off"
        return ViewerInspection(
            "verified", tuple(sorted(matches, key=lambda row: row.window_id)), close_safe, reason
        )
    reason = None if close_safe else "destroy-unattached is not provably off"
    if title_candidate and remote_route is not None:
        return ViewerInspection(
            "unverified", (), close_safe, "unmarked remote Kitty window is unverified"
        )
    return ViewerInspection("none", (), close_safe, reason, tuple(sorted(pending_launch_ids)))


def focus_window(window_id: int, *, niri_command: Sequence[str] = ("niri",)) -> bool:
    if type(window_id) is not int or window_id <= 0:
        return False
    if not os.environ.get("NIRI_SOCKET") or shutil.which(niri_command[0]) is None:
        return False
    try:
        result = subprocess.run(
            [*niri_command, "msg", "action", "focus-window", "--id", str(window_id)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def close_viewer(
    viewer: Viewer,
    *,
    revalidate: Callable[[], ViewerInspection],
    validate_session: Callable[[], bool],
    timeout_seconds: float = 2.0,
    niri_command: Sequence[str] = ("niri",),
) -> bool:
    """Close only the frozen attachment PID and verify window/session outcome."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ContractError("viewer_unsupported", "exact process signalling is unavailable")

    current = revalidate()
    if not current.close_safe:
        raise ContractError("viewer_destroy_guard", "destroy-unattached is not provably off")
    if current.status == "unsupported":
        raise ContractError("viewer_unsupported", current.reason or "viewer close is unsupported")
    if current.status == "ambiguous":
        raise ContractError("viewer_ambiguous", current.reason or "viewer identity is ambiguous")
    if current.status == "unverified":
        raise ContractError("viewer_unverified", current.reason or "viewer identity is unverified")
    if current.status == "none" or not any(
        row.viewer_id == viewer.viewer_id for row in current.viewers
    ):
        return False

    try:
        pidfd = os.pidfd_open(viewer.attachment_pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise ContractError(
            "viewer_close_ambiguous", "could not open an exact process handle"
        ) from error
    try:
        root = _proc(viewer.window_pid)
        attachment = _proc(viewer.attachment_pid)
        windows = _niri_windows(niri_command)
        if windows is None:
            raise ContractError(
                "viewer_close_ambiguous", "Niri window identity could not be rechecked"
            )
        if (
            root is None
            or attachment is None
            or root.start != viewer.window_start
            or attachment.start != viewer.attachment_start
            or attachment.ppid != viewer.window_pid
            or attachment.argv != viewer.attachment_argv
            or attachment.tty_nr == 0
            or not any(
                isinstance(row, dict)
                and row.get("id") == viewer.window_id
                and row.get("pid") == viewer.window_pid
                and row.get("app_id") == "kitty"
                for row in windows
            )
        ):
            if windows is not None and not any(
                isinstance(row, dict) and row.get("id") == viewer.window_id for row in windows
            ):
                return False
            if root is None or attachment is None:
                return False
            raise ContractError("viewer_stale", "viewer process identity changed before close")
        current = revalidate()
        if not current.close_safe:
            raise ContractError("viewer_destroy_guard", "destroy-unattached is not provably off")
        if current.status == "unsupported":
            raise ContractError(
                "viewer_unsupported", current.reason or "viewer close is unsupported"
            )
        if current.status == "ambiguous":
            raise ContractError(
                "viewer_ambiguous", current.reason or "viewer identity is ambiguous"
            )
        if current.status == "unverified":
            raise ContractError(
                "viewer_unverified", current.reason or "viewer identity is unverified"
            )
        if current.status == "none" or not any(
            row.viewer_id == viewer.viewer_id for row in current.viewers
        ):
            return False
        if not validate_session():
            raise ContractError("viewer_stale", "viewer or session identity changed before close")
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
        except OSError as error:
            raise ContractError(
                "viewer_close_ambiguous", "exact attachment signal failed"
            ) from error
    finally:
        os.close(pidfd)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        windows = _niri_windows(niri_command)
        if windows is None:
            raise ContractError(
                "viewer_close_ambiguous", "Niri window disappearance could not be verified"
            )
        remains = any(
            isinstance(row, dict)
            and row.get("id") == viewer.window_id
            and row.get("pid") == viewer.window_pid
            for row in windows
        )
        if not remains:
            if not validate_session():
                raise ContractError(
                    "viewer_close_ambiguous", "session survival could not be verified"
                )
            return True
        time.sleep(0.05)
    raise ContractError(
        "viewer_close_ambiguous", "Kitty window did not exit after its attachment closed"
    )


def _niri_windows(niri_command: Sequence[str]) -> list[object] | None:
    if not os.environ.get("NIRI_SOCKET") or shutil.which(niri_command[0]) is None:
        return None
    try:
        completed = subprocess.run(
            [*niri_command, "msg", "-j", "windows"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = completed.stdout
    if completed.returncode != 0 or not isinstance(raw, bytes) or len(raw) > _MAX_NIRI_BYTES:
        return None
    try:
        rows = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list) or len(rows) > 512:
        return None
    return rows


def _title_matches(value: object, session: Session, native_hostname: str | None = None) -> bool:
    if not isinstance(value, str) or not session.name:
        return False
    host = (native_hostname or os.uname().nodename).split(".", 1)[0].casefold()
    title = value.casefold()
    return title.startswith(f"{session.name.casefold()}:") and title.rstrip().endswith(f"@ {host}")


def _read_metadata(pid: int) -> tuple[dict[str, object] | None, bool]:
    try:
        fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY | os.O_CLOEXEC)
        try:
            raw = os.read(fd, _MAX_PROC_FILE + 1)
        finally:
            os.close(fd)
    except OSError:
        return None, False
    if len(raw) > _MAX_PROC_FILE:
        return None, False
    found = [
        part[len(_METADATA_ENV) + 1 :]
        for part in raw.split(b"\0")
        if part.startswith(_METADATA_ENV.encode() + b"=")
    ]
    if not found:
        return None, False
    if len(found) != 1:
        return None, True
    try:
        value = json.loads(found[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        return None, True
    return value, True


def _proc(pid: int) -> _Proc | None:
    try:
        raw_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = raw_stat.rfind(")")
        if close < 0:
            return None
        fields = raw_stat[close + 1 :].split()
        if len(fields) < 20:
            return None
        ppid, pgrp, session, tty_nr, start = (
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[4]),
            int(fields[19]),
        )
        fd = os.open(f"/proc/{pid}/cmdline", os.O_RDONLY | os.O_CLOEXEC)
        try:
            raw_cmdline = os.read(fd, 32 * 1024 + 1)
        finally:
            os.close(fd)
        if len(raw_cmdline) > 32 * 1024:
            return None
        argv = tuple(part.decode("utf-8", "replace") for part in raw_cmdline.split(b"\0") if part)
        return _Proc(pid, ppid, pgrp, session, tty_nr, start, argv)
    except (OSError, UnicodeError, ValueError):
        return None


def _process_tree(root_pid: int) -> tuple[list[_Proc], bool]:
    result: list[_Proc] = []
    queue: deque[tuple[int, int]] = deque([(root_pid, 0)])
    seen = {root_pid}
    while queue:
        pid, depth = queue.popleft()
        if pid != root_pid:
            proc = _proc(pid)
            if proc is None:
                return result, False
            result.append(proc)
        if depth >= _MAX_DEPTH or len(seen) >= _MAX_PROCESSES:
            return result, False
        try:
            raw_children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
        except OSError:
            return result, False
        for item in raw_children.split():
            try:
                child = int(item)
            except ValueError:
                return result, False
            if child in seen:
                return result, False
            seen.add(child)
            queue.append((child, depth + 1))
    return result, True


def _direct_children(root_pid: int) -> list[_Proc]:
    try:
        raw = Path(f"/proc/{root_pid}/task/{root_pid}/children").read_text(encoding="ascii")
    except OSError:
        return []
    result: list[_Proc] = []
    for item in raw.split():
        try:
            pid = int(item)
        except ValueError:
            return []
        proc = _proc(pid)
        if proc is not None:
            result.append(proc)
    return result


def _is_tmux_attach(argv: Sequence[str], session_id: str) -> bool:
    if not argv or Path(argv[0]).name != "tmux":
        return False
    return len(argv) >= 5 and "attach-session" in argv and argv[-2:] == ("-t", session_id)


def _remote_attach_argv(executable: str, route: str, session_id: str) -> tuple[str, ...]:
    # Match the process argv used by RemoteLifecycle._launch. OpenSSH receives
    # one remote shell command whose session ID is quoted as a literal target.
    import shlex

    remote = " ".join(
        shlex.quote(item) for item in ("tmux", "-u", "attach-session", "-t", session_id)
    )
    return (Path(executable).name, "-t", route, remote)


def _viewer_id(
    window_id: int,
    window_pid: int,
    window_start: int,
    attachment_pid: int,
    attachment_start: int,
    launch_id: str | None,
    session_ref: Mapping[str, object],
) -> str:
    frozen = {
        "windowId": window_id,
        "windowPid": window_pid,
        "windowStart": window_start,
        "attachmentPid": attachment_pid,
        "attachmentStart": attachment_start,
        "launchId": launch_id,
        "sessionRef": dict(session_ref),
    }
    digest = hashlib.sha256(
        json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    return _VIEWER_ID_PREFIX + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
