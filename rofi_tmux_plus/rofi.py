"""Rofi script-mode frontend for the Tmux Session Contract v1.

Rofi starts this process again for every script callback.  The frontend keeps
its small navigation/refresh state in typed JSON ``ROFI_DATA`` and keeps the
stable session reference in typed JSON ``ROFI_INFO``.  Visible row text is
never interpreted as an operation target.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from html import escape

from .config import Config, has_control, load_config
from .errors import ContractError, clean_message
from .lifecycle_service import LifecycleService
from .picker_model import PickerModelService
from .tmux import validate_session_id

ROFI_RETV_SELECTED = 1
ROFI_RETV_CUSTOM_INPUT = 2  # Ctrl+Enter / accept-custom
ROFI_RETV_DELETE_ENTRY = 3  # Shift+Delete
ROFI_RETV_CUSTOM_1 = 10  # Alt+R
ROFI_RETV_CUSTOM_2 = 11  # Right
ROFI_RETV_CUSTOM_3 = 12  # Left
ROFI_RETV_CUSTOM_4 = 13  # F2
ROFI_RETV_CUSTOM_6 = 15  # Escape
ROFI_RETV_CUSTOM_19 = 28  # timeout callback

MAX_MESSAGE_LENGTH = 360
MAX_DATA_LENGTH = 16 * 1024
MAX_TEXT_LENGTH = 16 * 1024
# Rofi state is JSON with ensure_ascii enabled. Keep typed names modest and
# reserve room for two escaped notices plus deadlines after a pending action.
MAX_TYPED_NAME_LENGTH = 2 * 1024
_ACTION_STATE_RESERVE = 2 * MAX_MESSAGE_LENGTH * 6 + 512
_MAX_ACTION_STATE_LENGTH = MAX_DATA_LENGTH - _ACTION_STATE_RESERVE
ERROR_NOTICE_SECONDS = 3
AUTO_REFRESH_POLL_SECONDS = 1
AUTO_REFRESH_MAX_SECONDS = 30

ROW_SEPARATOR = "\n"
# Rofi remembers this delimiter after the initial invocation. Use the same
# explicit escape form as its script-mode documentation and a character that
# cannot survive our sanitized dynamic row fields.
ROFI_RECORD_SEPARATOR = "\x1e"
ROFI_DELIMITER_VALUE = r"\x1e"
ROFI_INFO_KEY = "info"

VIEW_RECENT = "recent"
VIEW_HOSTS = "hosts"
_VIEWS = (VIEW_RECENT, VIEW_HOSTS)
_SESSION_ID = re.compile(r"^\$[0-9]+$")
_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)

# Generic icon-theme names: host and terminal are intentionally the only
# semantic categories in this generic multiplexer picker.
TERMINAL_ICON = "utilities-terminal-symbolic"
HOST_ICON = "network-server-symbolic"


def sanitize(value: object) -> str:
    """Make dynamic text safe for one-line Rofi protocol fields."""

    text = "" if value is None else str(value)
    return "".join(" " if has_control(character) else character for character in text).strip()


def _notice(value: object) -> str:
    text = sanitize(value)
    return text if len(text) <= MAX_MESSAGE_LENGTH else text[: MAX_MESSAGE_LENGTH - 1] + "…"


def _pango_escape(value: object) -> str:
    text = sanitize(value).replace("\u0085", " ").replace("\u2028", " ").replace("\u2029", " ")
    return escape(text, quote=False)


def _protocol(key: str, value: object) -> str:
    return "\0" + key + "\x1f" + sanitize(value)


def _row_options(options: Sequence[tuple[str, object]]) -> str:
    fields: list[str] = []
    for key, value in options:
        if key == "display":
            # The sole intentional LF is the visual line separator in the
            # Pango display option; it is not a Rofi record delimiter.
            encoded = (
                str(value).replace("\u0085", " ").replace("\u2028", " ").replace("\u2029", " ")
            )
        else:
            encoded = sanitize(value)
        fields.extend((sanitize(key), encoded))
    return "\0" + "\x1f".join(fields) if fields else ""


def _clean_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or value.startswith("-")
        or not _HOST_ID.fullmatch(value)
    ):
        raise ContractError("invalid_input", f"{field} is invalid")
    return value


def _clean_text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH or has_control(value):
        raise ContractError("invalid_input", f"{field} is invalid")
    return value


def _clean_timestamp(value: object, field: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError("invalid_input", f"{field} is invalid")
    return value


def _typed_name(value: object) -> str:
    """Validate a user-supplied tmux name before any lifecycle call."""

    name = _clean_text(value, "session name")
    assert name is not None
    name = name.strip()
    if not name:
        raise ContractError("invalid_input", "session name is empty")
    if len(name) > MAX_TYPED_NAME_LENGTH:
        raise ContractError("invalid_input", "session name is too large")
    return name


@dataclass(frozen=True, slots=True)
class NavigationState:
    """Current root and optional logical host layer."""

    view: str = VIEW_RECENT
    host_id: str | None = None

    def __post_init__(self) -> None:
        view = self.view if isinstance(self.view, str) and self.view in _VIEWS else VIEW_RECENT
        host_id = self.host_id
        if view != VIEW_HOSTS or not isinstance(host_id, str) or not host_id:
            host_id = None
        else:
            try:
                host_id = _clean_id(host_id, "host id")
            except ContractError:
                host_id = None
        object.__setattr__(self, "view", view)
        object.__setattr__(self, "host_id", host_id)

    @property
    def nested(self) -> bool:
        return self.view == VIEW_HOSTS and self.host_id is not None

    def root(self) -> NavigationState:
        return NavigationState(self.view)


@dataclass(frozen=True, slots=True)
class ActionState:
    """A typed, bounded non-browsing Rofi state.

    The selection is the complete session reference received from a row's
    JSON metadata.  It deliberately contains no visible display text.
    """

    kind: str
    origin: NavigationState
    name: str = ""
    selection: Mapping[str, object] | None = None


_ACTION_KINDS = frozenset({"choose-host", "rename", "confirm-kill"})
_INVALID_ACTION = object()


def _action_selection(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError("invalid_input", "Rofi action selection is invalid")
    selection = _session_info(value)
    name = selection.get("name")
    if not isinstance(name, str) or not name:
        raise ContractError("invalid_input", "Rofi action session name is invalid")
    attached = selection.get("attachedClients")
    if attached is not None and (
        isinstance(attached, bool) or not isinstance(attached, int) or attached < 0
    ):
        raise ContractError("invalid_input", "Rofi action client count is invalid")
    return {
        key: selection[key]
        for key in (
            "type",
            "meshRevision",
            "hostId",
            "serverGeneration",
            "sessionId",
            "createdAt",
            "name",
            "attachedClients",
        )
    }


def _action_payload(action: ActionState) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": action.kind,
        "origin": _navigation_payload(action.origin),
    }
    if action.name:
        value["name"] = action.name
    if action.selection is not None:
        value["selection"] = dict(action.selection)
    return value


def _strict_navigation_payload(value: object) -> NavigationState | None:
    if not isinstance(value, Mapping) or set(value) - {"view", "hostId"}:
        return None
    view = value.get("view")
    if not isinstance(view, str) or view not in _VIEWS:
        return None
    host_id = value.get("hostId")
    if view != VIEW_HOSTS:
        return NavigationState(view) if host_id is None else None
    if host_id is None:
        return NavigationState(view)
    try:
        return NavigationState(view, _clean_id(host_id, "host id"))
    except ContractError:
        return None


def _parse_action_payload(value: object) -> ActionState | object:
    if not isinstance(value, Mapping) or value.get("kind") not in _ACTION_KINDS:
        return _INVALID_ACTION
    origin = _strict_navigation_payload(value.get("origin"))
    if origin is None:
        return _INVALID_ACTION
    kind = str(value["kind"])
    try:
        if kind == "choose-host":
            if set(value) != {"kind", "origin", "name"}:
                return _INVALID_ACTION
            name = _typed_name(value.get("name"))
            action = ActionState(kind, origin, name=name)
        else:
            if set(value) != {"kind", "origin", "selection"}:
                return _INVALID_ACTION
            action = ActionState(kind, origin, selection=_action_selection(value.get("selection")))
        _validate_action_state(action)
        return action
    except ContractError:
        return _INVALID_ACTION


@dataclass(frozen=True, slots=True)
class ContinuationState:
    """The complete typed state carried between script-mode callbacks."""

    navigation: NavigationState = NavigationState()
    refresh_deadline: float | None = None
    error_deadline: float | None = None
    error_message: str = ""
    notice_key: str = ""
    action: ActionState | None = None
    blocked_action: bool = False

    @property
    def has_lifecycle(self) -> bool:
        return self.refresh_deadline is not None or self.error_deadline is not None

    def active(self, now: float | None = None) -> ContinuationState:
        current = time.time() if now is None else now

        def live(deadline: float | None) -> float | None:
            if deadline is None:
                return None
            try:
                return deadline if math.isfinite(deadline) and deadline > current else None
            except (TypeError, ValueError, OverflowError):
                return None

        error_deadline = live(self.error_deadline)
        # Keep notice_key after expiry.  It prevents a failed marker from
        # creating a fresh notice on every one-second timeout callback.
        return replace(
            self,
            refresh_deadline=live(self.refresh_deadline),
            error_deadline=error_deadline,
            error_message=self.error_message if error_deadline is not None else "",
        )


def _navigation_payload(navigation: NavigationState) -> dict[str, object]:
    result: dict[str, object] = {"view": navigation.view}
    if navigation.nested:
        result["hostId"] = navigation.host_id
    return result


def _state_payload(state: ContinuationState) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "navigation": _navigation_payload(state.navigation),
    }
    if state.refresh_deadline is not None:
        payload["refreshDeadline"] = max(0, int(state.refresh_deadline))
    if state.error_deadline is not None:
        payload["errorDeadline"] = max(0, int(state.error_deadline))
        if state.error_message:
            payload["errorMessage"] = _notice(state.error_message)
    if state.notice_key:
        payload["noticeKey"] = sanitize(state.notice_key)[:MAX_MESSAGE_LENGTH]
    if state.action is not None:
        payload["action"] = _action_payload(state.action)
    if state.blocked_action:
        payload["blockedAction"] = True
    return payload


def _state_data(state: ContinuationState) -> str:
    encoded = json.dumps(_state_payload(state), ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > MAX_DATA_LENGTH:
        raise ContractError("invalid_input", "Rofi continuation state is too large")
    return encoded


def _validate_action_state(action: ActionState) -> None:
    encoded = json.dumps(
        _state_payload(ContinuationState(action=action)), ensure_ascii=True, separators=(",", ":")
    )
    if len(encoded) > _MAX_ACTION_STATE_LENGTH:
        raise ContractError("invalid_input", "Rofi pending action is too large")


def _new_action(
    kind: str,
    origin: NavigationState,
    *,
    name: str = "",
    selection: Mapping[str, object] | None = None,
) -> ActionState:
    action = ActionState(kind, origin, name=name, selection=selection)
    _validate_action_state(action)
    return action


def _navigation_data(navigation: NavigationState) -> str:
    return _state_data(ContinuationState(navigation=navigation))


def _refresh_data(
    refresh_deadline: float | None = None,
    error_deadline: float | None = None,
    error_message: str = "",
    *,
    deadline: float | None = None,
    navigation: NavigationState | None = None,
    notice_key: str = "",
) -> str:
    if refresh_deadline is None:
        refresh_deadline = deadline
    return _state_data(
        ContinuationState(
            navigation=navigation or NavigationState(),
            refresh_deadline=refresh_deadline,
            error_deadline=error_deadline,
            error_message=error_message,
            notice_key=notice_key,
        )
    )


def _parse_deadline(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        deadline = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return deadline if math.isfinite(deadline) and deadline > 0 else None


def _parse_navigation_payload(value: object) -> NavigationState:
    if not isinstance(value, Mapping):
        return NavigationState()
    view = value.get("view")
    host_id = value.get("hostId")
    return NavigationState(view, host_id if isinstance(host_id, str) else None)


def _parse_continuation_state(value: object) -> ContinuationState:
    if value is None:
        return ContinuationState()
    if not isinstance(value, str) or len(value) > MAX_DATA_LENGTH:
        return ContinuationState(blocked_action=True)
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        if not value.startswith("tmux-state:"):
            return ContinuationState(blocked_action=True)
        try:
            payload = json.loads(value.removeprefix("tmux-state:"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ContinuationState(blocked_action=True)
    if (
        not isinstance(payload, Mapping)
        or isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
    ):
        return ContinuationState(blocked_action=True)
    action: ActionState | None = None
    blocked = payload.get("blockedAction") is True
    if "blockedAction" in payload and payload.get("blockedAction") is not True:
        blocked = True
    if "action" in payload:
        parsed_action = _parse_action_payload(payload.get("action"))
        if parsed_action is _INVALID_ACTION:
            blocked = True
        else:
            action = parsed_action  # type: ignore[assignment]
    if blocked:
        action = None
    return ContinuationState(
        navigation=_parse_navigation_payload(payload.get("navigation")),
        refresh_deadline=_parse_deadline(payload.get("refreshDeadline")),
        error_deadline=_parse_deadline(payload.get("errorDeadline")),
        error_message=_notice(payload.get("errorMessage"))
        if isinstance(payload.get("errorMessage"), str)
        else "",
        notice_key=sanitize(payload.get("noticeKey"))[:MAX_MESSAGE_LENGTH]
        if isinstance(payload.get("noticeKey"), str)
        else "",
        action=action,
        blocked_action=blocked,
    )


parse_continuation_state = _parse_continuation_state


def _timeout_theme(delay: float | bool | None = None, *, enabled: bool | None = None) -> str:
    if delay is None and enabled is not None:
        delay = enabled
    if isinstance(delay, bool):
        delay = AUTO_REFRESH_POLL_SECONDS if delay else 0
    normalized = max(0, int(delay or 0))
    return f'configuration {{ timeout {{ delay: {normalized}; action: "kb-custom-19"; }} }}'


def _age(timestamp: object, now: float | None = None) -> str:
    try:
        value = float(timestamp)
        if not math.isfinite(value) or value <= 0:
            return "unknown"
        seconds = max(0, int((time.time() if now is None else now) - value))
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


def _shorten_path(value: object, width: int = 48) -> str:
    path = sanitize(value)
    if not path:
        return "~"
    home = os.path.expanduser("~")
    if path == home:
        path = "~"
    elif path.startswith(home + "/"):
        path = "~" + path[len(home) :]
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :]


def _host_catalog(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    raw = payload.get("hostCatalog", []) if isinstance(payload, Mapping) else []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            host_id = _clean_id(item.get("hostId"), "host id")
            display = _clean_text(item.get("display"), "host display")
        except ContractError:
            continue
        if display is None or not isinstance(item.get("local"), bool):
            continue
        if host_id.casefold() in seen:
            continue
        seen.add(host_id.casefold())
        result.append({"hostId": host_id, "display": display, "local": item["local"]})
    return result


def _host_rows(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    raw = payload.get("hosts", []) if isinstance(payload, Mapping) else []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            host_id = _clean_id(item.get("hostId"), "host id")
        except ContractError:
            continue
        result.append({**dict(item), "hostId": host_id})
    return result


def _host_for(payload: Mapping[str, object] | None, host_id: str) -> dict[str, object] | None:
    key = host_id.casefold()
    return next((row for row in _host_rows(payload) if row["hostId"].casefold() == key), None)


def _session_rows(
    payload: Mapping[str, object] | None, *, host_id: str | None = None
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for host in _host_rows(payload):
        if host_id is not None and host["hostId"].casefold() != host_id.casefold():
            continue
        sessions = host.get("sessions")
        if not isinstance(sessions, list):
            continue
        for value in sessions:
            if not isinstance(value, Mapping) or value.get("hostId") != host["hostId"]:
                continue
            session = dict(value)
            if not isinstance(session.get("sessionId"), str) or not _SESSION_ID.fullmatch(
                session["sessionId"]
            ):
                continue
            if isinstance(session.get("createdAt"), bool) or not isinstance(
                session.get("createdAt"), int
            ):
                continue
            session["_host"] = host
            result.append(session)
    return result


def _host_live(host: Mapping[str, object]) -> bool:
    return (
        host.get("status") == "ok"
        and host.get("stale") is not True
        and host.get("unavailable") is not True
    )


def _recency(session: Mapping[str, object]) -> int | None:
    for field in ("activityAt", "lastAttachedAt", "createdAt"):
        value = session.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _session_sort_key(
    session: Mapping[str, object], host_order: Mapping[str, int]
) -> tuple[object, ...]:
    host = session.get("_host")
    host_mapping = host if isinstance(host, Mapping) else {}
    host_id = str(session.get("hostId") or "")
    timestamp = _recency(session)
    name = sanitize(session.get("name") or session.get("sessionId") or "session")
    return (
        0 if _host_live(host_mapping) else 1,
        0 if timestamp is not None else 1,
        -(timestamp or 0),
        host_order.get(host_id.casefold(), 2**31),
        host_id.casefold(),
        host_id,
        name.casefold(),
        name,
        str(session.get("sessionId") or "").casefold(),
        str(session.get("sessionId") or ""),
    )


def _niri_titles() -> tuple[str, ...]:
    """Read current Niri titles once per render, best effort only."""

    if not os.environ.get("NIRI_SOCKET") or shutil.which("niri") is None:
        return ()
    try:
        result = subprocess.run(
            ["niri", "msg", "-j", "windows"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
            check=False,
        )
        if result.returncode:
            return ()
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(
        item["title"]
        for item in value
        if isinstance(item, Mapping) and isinstance(item.get("title"), str)
    )


def _is_open_here(
    session: Mapping[str, object], host: Mapping[str, object], titles: Sequence[str]
) -> bool:
    if not host.get("local") or not _host_live(host):
        return False
    name = session.get("name")
    native = host.get("nativeHostname")
    if not isinstance(name, str) or not name or not isinstance(native, str) or not native:
        return False
    prefix = name.casefold() + ":"
    suffix = "@ " + native.split(".", 1)[0].casefold()
    return any(
        title.casefold().startswith(prefix) and title.rstrip().endswith(suffix) for title in titles
    )


def _session_status(
    session: Mapping[str, object], host: Mapping[str, object], titles: Sequence[str]
) -> str:
    if not _host_live(host):
        return "unavailable"
    if _is_open_here(session, host, titles):
        return "open here"
    attached = session.get("attachedClients")
    if isinstance(attached, int) and not isinstance(attached, bool) and attached > 0:
        return "attached"
    return "detached"


def _session_payload(
    session: Mapping[str, object],
    host: Mapping[str, object],
    status: str,
    mesh_revision: str | None,
) -> dict[str, object]:
    return {
        "type": "session",
        "meshRevision": mesh_revision,
        "hostId": host.get("hostId"),
        "serverGeneration": session.get("serverGeneration"),
        "sessionId": session.get("sessionId"),
        "createdAt": session.get("createdAt"),
        "name": session.get("name"),
        "attachedClients": session.get("attachedClients"),
        "windowCount": session.get("windowCount"),
        "status": status,
        "display": host.get("display"),
        "sessionPath": session.get("sessionPath"),
        "currentWindow": session.get("currentWindow"),
        "currentPath": session.get("currentPath"),
    }


def selection_payload(
    session: Mapping[str, object], *, status: str | None = None, mesh_revision: str | None
) -> str:
    host = session.get("_host")
    host_mapping = host if isinstance(host, Mapping) else {}
    selected_status = status or str(session.get("status") or "detached")
    return json.dumps(
        _session_payload(session, host_mapping, selected_status, mesh_revision),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _session_info(value: Mapping[str, object]) -> dict[str, object]:
    kind = value.get("type")
    if kind is not None and kind != "session":
        raise ContractError("invalid_input", "Rofi selection is not a session")
    host_id = _clean_id(value.get("hostId"), "session host id")
    if "meshRevision" not in value:
        raise ContractError("invalid_input", "Rofi selection mesh revision is missing")
    mesh_revision = _clean_text(value.get("meshRevision"), "session mesh revision", nullable=True)
    generation = _clean_text(value.get("serverGeneration"), "server generation")
    session_id = _clean_text(value.get("sessionId"), "session id")
    if not generation or not session_id:
        raise ContractError("invalid_input", "Rofi selection identity is incomplete")
    try:
        validate_session_id(session_id)
    except ContractError as error:
        raise ContractError("invalid_input", "Rofi selection has an invalid session id") from error
    created_at = _clean_timestamp(value.get("createdAt"), "createdAt")
    assert created_at is not None
    name = _clean_text(value.get("name"), "session name", nullable=True)
    result = dict(value)
    result.update(
        {
            "type": "session",
            "meshRevision": mesh_revision,
            "hostId": host_id,
            "serverGeneration": generation,
            "sessionId": session_id,
            "createdAt": created_at,
            "name": name,
        }
    )
    return result


def _host_info(value: Mapping[str, object]) -> dict[str, object]:
    display = _clean_text(value.get("display"), "host display")
    if display is None:
        raise ContractError("invalid_input", "host display is invalid")
    return {
        "type": "host",
        "hostId": _clean_id(value.get("hostId"), "host id"),
        "display": display,
    }


def _parse_row_selection(raw: str | None) -> tuple[str, dict[str, object]]:
    if not raw or len(raw) > MAX_DATA_LENGTH:
        raise ContractError("invalid_input", "Rofi did not provide row metadata")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ContractError("invalid_input", "Rofi row metadata is invalid") from error
    if not isinstance(value, Mapping):
        raise ContractError("invalid_input", "Rofi row metadata is not an object")
    if value.get("type") == "host":
        return "host", _host_info(value)
    return "session", _session_info(value)


def _host_display(payload: Mapping[str, object] | None, host_id: str | None) -> str:
    if not host_id:
        return ""
    for host in _host_catalog(payload):
        if host["hostId"].casefold() == host_id.casefold():
            return str(host["display"])
    row = _host_for(payload, host_id)
    return str(row.get("display", host_id)) if row is not None else host_id


def _payload_errors(payload: Mapping[str, object] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    errors: list[str] = []
    for host in _host_rows(payload):
        error = host.get("error")
        if not isinstance(error, Mapping):
            continue
        message = sanitize(error.get("message") or host.get("status") or "failed")
        if message:
            errors.append(f"{sanitize(host.get('display') or host.get('hostId'))}: {message}")
    if not errors:
        return ""
    value = "Refresh errors: " + "; ".join(errors)
    return value if len(value) <= MAX_MESSAGE_LENGTH else value[: MAX_MESSAGE_LENGTH - 1] + "…"


def _session_secondary(
    session: Mapping[str, object], host: Mapping[str, object], status: str, now: float
) -> str:
    parts: list[str] = []
    if not host.get("_scope"):
        parts.append(sanitize(host.get("display") or host.get("hostId") or "host"))
    window_count = session.get("windowCount")
    windows = f"{window_count} windows" if isinstance(window_count, int) else "windows unknown"
    parts.extend(
        (
            _shorten_path(session.get("sessionPath") or session.get("currentPath")),
            sanitize(session.get("currentWindow") or "window"),
            windows,
            status,
            "activity " + _age(_recency(session), now),
        )
    )
    return "  ·  ".join(parts)


def _session_display(
    session: Mapping[str, object], host: Mapping[str, object], status: str, now: float
) -> str:
    name = sanitize(session.get("name") or session.get("sessionId") or "tmux session")
    secondary = _session_secondary(session, host, status, now)
    return (
        f"<b>{_pango_escape(name)}</b>"
        f'{ROW_SEPARATOR}<span size="smaller" alpha="78%">'
        f"{_pango_escape(secondary)}</span>"
    )


def _host_group_display(
    host: Mapping[str, object], sessions: Sequence[Mapping[str, object]], *, now: float
) -> str:
    label = sanitize(host.get("display") or host.get("hostId") or "host")
    count = len(sessions)
    noun = "session" if count == 1 else "sessions"
    row = host.get("_row")
    host_row = row if isinstance(row, Mapping) else {}
    if host_row.get("status") in {"unreachable", "error"} or host_row.get("unavailable"):
        suffix = f"{count} {noun}  ·  unavailable"
    elif host_row.get("status") == "tmux_missing":
        suffix = f"{count} {noun}  ·  tmux unavailable"
    elif sessions:
        newest = max((_recency(item) or 0 for item in sessions), default=0)
        suffix = f"{count} {noun}  ·  newest {_age(newest, now)}"
    else:
        suffix = f"{count} {noun}  ·  no sessions"
    return (
        f'<b>{_pango_escape(label)}</b><span alpha="60%">  ›</span>'
        f'{ROW_SEPARATOR}<span size="smaller" alpha="78%">'
        f"{_pango_escape(suffix)}</span>"
    )


def _host_rows_render(payload: Mapping[str, object] | None, *, now: float) -> list[str]:
    rows: list[str] = []
    for host in _host_catalog(payload):
        host_id = str(host["hostId"])
        sessions = _session_rows(payload, host_id=host_id)
        row_host = {**host, "_row": _host_for(payload, host_id)}
        info = json.dumps(_host_info(host), ensure_ascii=True, separators=(",", ":"))
        metadata = " ".join((host_id, sanitize(host["display"]), "host", "tmux"))
        label = sanitize(host["display"])
        rows.append(
            label
            + _row_options(
                (
                    (ROFI_INFO_KEY, info),
                    ("meta", metadata),
                    ("icon", HOST_ICON),
                    ("display", _host_group_display(row_host, sessions, now=now)),
                )
            )
        )
    return rows


def _session_rows_render(
    payload: Mapping[str, object] | None,
    navigation: NavigationState,
    *,
    now: float,
    titles: Sequence[str],
) -> list[str]:
    catalog = _host_catalog(payload)
    candidate_revision = payload.get("meshRevision") if isinstance(payload, Mapping) else None
    mesh_revision = candidate_revision if isinstance(candidate_revision, str) else None
    order = {str(host["hostId"]).casefold(): index for index, host in enumerate(catalog)}
    sessions = _session_rows(payload, host_id=navigation.host_id if navigation.nested else None)
    sessions.sort(key=lambda item: _session_sort_key(item, order))
    rows: list[str] = []
    for session in sessions:
        host = session.get("_host")
        if not isinstance(host, Mapping):
            continue
        scoped_host = {**dict(host), "_scope": navigation.nested}
        status = _session_status(session, scoped_host, titles)
        info = selection_payload(session, status=status, mesh_revision=mesh_revision)
        metadata = " ".join(
            sanitize(value)
            for value in (
                session.get("hostId"),
                host.get("display"),
                session.get("name"),
                session.get("sessionPath"),
                session.get("currentPath"),
                session.get("currentWindow"),
                status,
                session.get("sessionId"),
            )
            if value is not None
        )
        primary = sanitize(session.get("name") or session.get("sessionId") or "tmux session")
        rows.append(
            primary
            + _row_options(
                (
                    (ROFI_INFO_KEY, info),
                    ("meta", metadata),
                    ("icon", TERMINAL_ICON),
                    ("display", _session_display(session, scoped_host, status, now)),
                )
            )
        )
    return rows


def _prompt(
    payload: Mapping[str, object] | None,
    navigation: NavigationState,
    action: ActionState | None = None,
) -> str:
    if action is not None:
        if action.kind == "choose-host":
            return "Tmux › Choose host"
        if action.kind == "rename":
            return "Tmux › Rename session"
        return "Tmux › Confirm kill"
    if navigation.nested:
        return "Tmux › Hosts › " + sanitize(_host_display(payload, navigation.host_id))
    return "Tmux › " + ("Recent" if navigation.view == VIEW_RECENT else "Hosts")


def render_snapshot(
    snapshot: Mapping[str, object] | None,
    *,
    message: str = "",
    selected: Mapping[str, object] | None = None,
    preserve: bool = False,
    now: float | None = None,
    continuation: bool = False,
    timeout: bool | None = None,
    refresh_deadline: float | None = None,
    error_deadline: float | None = None,
    error_message: str = "",
    notice_key: str = "",
    clear_message: bool = False,
    navigation: NavigationState | None = None,
    state: ContinuationState | None = None,
    titles: Sequence[str] | None = None,
) -> str:
    """Render a model payload using Rofi's script-mode protocol."""

    del selected  # Kept as a compatibility parameter; identity is typed info.
    if state is None:
        state = ContinuationState(
            navigation=navigation or NavigationState(),
            refresh_deadline=refresh_deadline,
            error_deadline=error_deadline,
            error_message=error_message,
            notice_key=notice_key,
        )
    elif navigation is not None and navigation != state.navigation:
        state = replace(state, navigation=navigation)
    now_value = time.time() if now is None else now
    active = state.active(now_value)
    headers = [
        _protocol("prompt", _prompt(snapshot, active.navigation, active.action)),
        _protocol("use-hot-keys", "true"),
        _protocol("markup-rows", "true"),
    ]
    # Only the session browse surfaces accept typed create/open input.  Rofi
    # script mode has no safe header for pre-populating an input field, so the
    # rename screen displays the current name and accepts custom input only.
    allow_custom = active.action is not None and active.action.kind == "rename"
    allow_custom = allow_custom or (
        active.action is None
        and (active.navigation.view == VIEW_RECENT or active.navigation.nested)
    )
    headers.append(_protocol("no-custom", "false" if allow_custom else "true"))
    if preserve:
        headers.extend((_protocol("keep-selection", "true"), _protocol("keep-filter", "true")))
    effective_message = _notice(message)
    if not effective_message and not clear_message:
        effective_message = active.error_message
    if not effective_message and not clear_message and active.refresh_deadline is not None:
        effective_message = "Refreshing in background"
    if effective_message or clear_message:
        headers.append(_protocol("message", effective_message))
    lifecycle_was_present = state.has_lifecycle
    lifecycle_is_present = active.has_lifecycle
    if timeout is None:
        if lifecycle_is_present:
            timeout = True
        elif lifecycle_was_present:
            timeout = False
    if timeout is not None:
        if timeout:
            if active.error_deadline is not None and active.refresh_deadline is None:
                delay = max(1, math.ceil(active.error_deadline - now_value))
            else:
                delay = AUTO_REFRESH_POLL_SECONDS
            headers.append(_protocol("theme", _timeout_theme(delay)))
            headers.append(_protocol("data", _state_data(active)))
        else:
            headers.append(_protocol("theme", _timeout_theme(0)))
            headers.append(_protocol("data", _state_data(replace(active, refresh_deadline=None))))
    elif continuation:
        headers.append(_protocol("data", _state_data(active)))

    if active.action is not None and active.action.kind == "choose-host":
        rows = _host_rows_render(snapshot, now=now_value)
    elif active.action is not None and active.action.kind == "rename":
        selection = active.action.selection or {}
        current_name = sanitize(selection.get("name") or "session")
        text = "Enter a new name with Ctrl+Enter"
        rows = [
            current_name
            + _row_options(
                (
                    ("nonselectable", "true"),
                    ("display", f"<b>{_pango_escape(current_name)}</b>" + ROW_SEPARATOR + text),
                )
            )
        ]
    elif active.action is not None and active.action.kind == "confirm-kill":
        selection = active.action.selection or {}
        host_label = _host_display(snapshot, str(selection.get("hostId") or ""))
        name = sanitize(selection.get("name") or "session")
        attached = selection.get("attachedClients")
        impact = (
            f"disconnects {attached} live client{'s' if attached != 1 else ''}"
            if isinstance(attached, int) and attached >= 0
            else "live client count unavailable"
        )
        cancel = json.dumps({"type": "cancel"}, separators=(",", ":"))
        kill = json.dumps(
            {"type": "kill", "selection": dict(selection)}, ensure_ascii=True, separators=(",", ":")
        )
        rows = [
            "Cancel"
            + _row_options(
                (
                    (ROFI_INFO_KEY, cancel),
                    ("display", "<b>Cancel</b>" + ROW_SEPARATOR + "Keep this session"),
                )
            ),
            "Kill "
            + name
            + _row_options(
                (
                    (ROFI_INFO_KEY, kill),
                    ("urgent", "true"),
                    ("icon", "edit-delete-symbolic"),
                    (
                        "display",
                        f"<b>Kill {_pango_escape(name)}</b>"
                        + ROW_SEPARATOR
                        + _pango_escape(f"{host_label} · {impact}"),
                    ),
                )
            ),
        ]
    elif active.navigation.view == VIEW_HOSTS and not active.navigation.nested:
        rows = _host_rows_render(snapshot, now=now_value)
    else:
        rows = _session_rows_render(
            snapshot,
            active.navigation,
            now=now_value,
            titles=titles if titles is not None else _niri_titles(),
        )
    if not rows:
        text = "No tmux sessions found" if active.navigation.nested else "No tmux sessions"
        rows = [
            text
            + _row_options(
                (
                    ("nonselectable", "true"),
                    ("urgent", "true"),
                    ("display", text + ROW_SEPARATOR + "No sessions available"),
                )
            )
        ]
    if continuation:
        return ROFI_RECORD_SEPARATOR.join((*headers, *rows)) + ROFI_RECORD_SEPARATOR
    # Change Rofi's record delimiter after the initial LF-delimited headers;
    # the display option can then contain its one intentional visual LF.
    headers.append(_protocol("delim", ROFI_DELIMITER_VALUE))
    return "\n".join(headers) + "\n" + ROFI_RECORD_SEPARATOR.join(rows) + ROFI_RECORD_SEPARATOR


def _render_state(
    payload: Mapping[str, object] | None,
    state: ContinuationState,
    *,
    message: str = "",
    preserve: bool = False,
    continuation: bool = True,
    timeout: bool | None = None,
    clear_message: bool = False,
    now: float | None = None,
) -> str:
    return render_snapshot(
        payload,
        message=message,
        preserve=preserve,
        continuation=continuation,
        timeout=timeout,
        clear_message=clear_message,
        state=state,
        now=now,
    )


def _error_state(
    state: ContinuationState, message: str, *, now: float, key: str
) -> ContinuationState:
    text = _notice(message)
    return replace(
        state,
        error_deadline=now + ERROR_NOTICE_SECONDS,
        error_message=text,
        notice_key=key + ":" + text,
    )


def _marker_key(marker: Mapping[str, object]) -> str:
    return ":".join(
        (
            sanitize(marker.get("state")),
            sanitize(marker.get("updatedAt")),
            sanitize(marker.get("message")),
        )
    )


def _refresh_observation(
    payload: Mapping[str, object] | None, state: ContinuationState, *, now: float
) -> tuple[ContinuationState, str]:
    marker = payload.get("remoteRefresh") if isinstance(payload, Mapping) else None
    marker_mapping = marker if isinstance(marker, Mapping) else None
    requested = (
        bool(payload.get("remoteRefreshRequested")) if isinstance(payload, Mapping) else False
    )
    state = state.active(now)
    if marker_mapping is not None:
        marker_state = marker_mapping.get("state")
        if marker_state == "running":
            return replace(
                state, refresh_deadline=state.refresh_deadline or now + AUTO_REFRESH_MAX_SECONDS
            ), "Refreshing in background"
        if marker_state in {"failed", "stale", "stalled"}:
            key = "refresh:" + _marker_key(marker_mapping)
            if marker_state == "failed":
                fallback = "background refresh failed"
            elif marker_state == "stale":
                fallback = "background refresh stopped because the Host Mesh changed"
            else:
                fallback = "background refresh stopped after it stalled"
            text = _notice(marker_mapping.get("message")) or fallback
            if state.notice_key == key:
                return replace(state, refresh_deadline=None), ""
            return (
                replace(
                    state,
                    refresh_deadline=None,
                    error_deadline=now + ERROR_NOTICE_SECONDS,
                    error_message=text,
                    notice_key=key,
                ),
                text,
            )
        if marker_state == "complete":
            return replace(state, refresh_deadline=None), ""
    if requested:
        return replace(
            state, refresh_deadline=state.refresh_deadline or now + AUTO_REFRESH_MAX_SECONDS
        ), "Refreshing in background"
    if state.refresh_deadline is not None:
        if now < state.refresh_deadline:
            return state, "Refreshing in background"
        key = "refresh:missing"
        if state.notice_key != key:
            text = "background refresh stopped without a result"
            return (
                replace(
                    state,
                    refresh_deadline=None,
                    error_deadline=now + ERROR_NOTICE_SECONDS,
                    error_message=text,
                    notice_key=key,
                ),
                text,
            )
        return replace(state, refresh_deadline=None), ""
    return state, ""


def _load_payload(model_service: PickerModelService, *, start_refresh: bool) -> dict[str, object]:
    model = model_service.load(start_refresh=start_refresh)
    payload = getattr(model, "payload", model)
    if not isinstance(payload, Mapping):
        raise ContractError("operation_failed", "picker model returned an invalid payload")
    return dict(payload)


def _stale(error: object) -> bool:
    return isinstance(error, ContractError) and error.code in {"stale_session", "stale_mesh"}


def _open_selection(
    selection: Mapping[str, object],
    lifecycle: LifecycleService,
) -> None:
    value = _session_info(selection)
    lifecycle.open(
        str(value["hostId"]),
        value["meshRevision"] if isinstance(value["meshRevision"], str) else None,
        str(value["serverGeneration"]),
        str(value["sessionId"]),
        int(value["createdAt"]),
        None,
    )


def _root_cycle(navigation: NavigationState, direction: int) -> NavigationState:
    index = _VIEWS.index(navigation.view) if navigation.view in _VIEWS else 0
    return NavigationState(_VIEWS[(index + direction) % len(_VIEWS)])


def _load_observed(
    model_service: PickerModelService, state: ContinuationState, *, start_refresh: bool, now: float
) -> tuple[dict[str, object], ContinuationState, str]:
    payload = _load_payload(model_service, start_refresh=start_refresh)
    next_state, message = _refresh_observation(payload, state, now=now)
    errors = _payload_errors(payload)
    if not message and not next_state.has_lifecycle and errors:
        key = "payload:" + errors
        if next_state.notice_key != key:
            next_state = _error_state(next_state, errors, now=now, key="payload")
            message = errors
    return payload, next_state, message


def _auto_refresh_callback(
    model_service: PickerModelService, state: ContinuationState, *, now: float
) -> str:
    try:
        payload, next_state, message = _load_observed(
            model_service, state, start_refresh=False, now=now
        )
    except Exception as error:  # noqa: BLE001 - visible script callback boundary
        next_state = _error_state(
            state, f"Refresh failed: {clean_message(error)}", now=now, key="callback"
        )
        return _render_state(None, next_state, preserve=True, now=now)
    timeout = next_state.has_lifecycle
    if not timeout and state.has_lifecycle:
        timeout = False
    return _render_state(
        payload,
        next_state,
        message=message,
        preserve=True,
        timeout=timeout,
        clear_message=not bool(message),
        now=now,
    )


def _forced_refresh(model_service: PickerModelService) -> dict[str, object]:
    method = getattr(model_service, "refresh_now", None)
    if callable(method):
        result = method()
        payload = getattr(result, "payload", result)
        if isinstance(payload, Mapping):
            return dict(payload)
    return _load_payload(model_service, start_refresh=False)


def _mesh_revision(payload: Mapping[str, object]) -> str | None:
    value = payload.get("meshRevision")
    return value if isinstance(value, str) else None


def _require_action_snapshot(
    payload: Mapping[str, object], selection: Mapping[str, object]
) -> None:
    """Require a pending destructive reference to retain its rendered Mesh."""

    if selection.get("meshRevision") != _mesh_revision(payload):
        raise ContractError("stale_mesh", "the selected host mesh changed; refresh and try again")
    host_id = str(selection.get("hostId") or "")
    if not any(item["hostId"].casefold() == host_id.casefold() for item in _host_catalog(payload)):
        raise ContractError("stale_mesh", "the selected host is no longer available")


def _ensure_open_or_create(
    payload: Mapping[str, object], host_id: str, name: str, lifecycle: LifecycleService
) -> None:
    """Open a currently live exact-name session, or make one on this host."""

    revision = _mesh_revision(payload)
    row = _host_for(payload, host_id)
    if row is not None and _host_live(row):
        for session in _session_rows(payload, host_id=host_id):
            if session.get("name") != name:
                continue
            selection = _session_info(
                _session_payload(session, row, _session_status(session, row, ()), revision)
            )
            _open_selection(selection, lifecycle)
            return
    lifecycle.create(host_id, revision, name, None, (), (), False, None, True)


def _host_refresh_error(payload: Mapping[str, object] | None, host_id: str) -> str:
    row = _host_for(payload, host_id)
    if row is None or _host_live(row):
        return ""
    error = row.get("error")
    if isinstance(error, Mapping):
        detail = _notice(error.get("message"))
    else:
        detail = _notice(row.get("status") or "refresh did not return a live host")
    return "Refresh warning: " + (detail or "host inventory is unavailable")


def _refresh_affected(
    model_service: PickerModelService,
    selection: Mapping[str, object],
    *,
    current: bool = False,
) -> dict[str, object]:
    host_id = str(selection["hostId"])
    if current:
        method = getattr(model_service, "refresh_host_current", None)
        if callable(method):
            result = method(host_id)
        else:
            # Production model always implements this. The fallback retains
            # backwards-compatible deterministic fakes without broad refresh.
            result = model_service.refresh_host(host_id, None)  # type: ignore[attr-defined]
    else:
        result = model_service.refresh_host(  # type: ignore[attr-defined]
            host_id, selection["meshRevision"]
        )
    payload = getattr(result, "payload", result)
    if not isinstance(payload, Mapping):
        raise ContractError("operation_failed", "affected-host refresh returned an invalid model")
    return dict(payload)


def _mutation_success(
    model_service: PickerModelService,
    state: ContinuationState,
    selection: Mapping[str, object],
    message: str,
    *,
    now: float,
    fallback_payload: Mapping[str, object] | None = None,
) -> tuple[dict[str, object] | None, ContinuationState, str]:
    """Leave an action after success; reconciliation can only add a warning."""

    origin = state.action.origin if state.action is not None else state.navigation
    next_state = replace(state, navigation=origin, action=None)
    # Mutation success must keep the picker useful even when the bounded
    # follow-up observation fails. This is an old browse snapshot, never a
    # claim that the mutation itself has been re-observed.
    payload: dict[str, object] | None = (
        dict(fallback_payload) if isinstance(fallback_payload, Mapping) else None
    )
    warning = ""
    try:
        payload = _refresh_affected(model_service, selection)
        warning = _host_refresh_error(payload, str(selection["hostId"]))
    except Exception as error:  # noqa: BLE001 - mutation has already succeeded
        if _stale(error):
            try:
                payload = _refresh_affected(model_service, selection, current=True)
            except Exception as current_error:  # noqa: BLE001 - bounded reconciliation warning
                warning = "Refresh warning: " + _notice(clean_message(current_error))
            else:
                warning = "Refresh warning: the Host Mesh changed while refreshing"
        else:
            warning = "Refresh warning: " + _notice(clean_message(error))
    text = message if not warning else message + " " + warning
    return payload, _error_state(next_state, text, now=now, key="mutation"), text


def _action_failure(
    model_service: PickerModelService,
    state: ContinuationState,
    error: object,
    *,
    verb: str,
    now: float,
) -> tuple[dict[str, object] | None, ContinuationState, str]:
    """Keep the exact action state and optionally re-read only its host."""

    payload: dict[str, object] | None = None
    if state.action is not None and state.action.selection is not None and _stale(error):
        try:
            payload = _refresh_affected(model_service, state.action.selection, current=True)
        except Exception:  # noqa: BLE001, S110 - the original action error is authoritative
            pass
    if payload is None:
        try:
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
            state = replace(observed, action=state.action)
        except Exception:  # noqa: BLE001, S110 - retain the original visible action error
            pass
    text = f"Unable to {verb}: {clean_message(error)}"
    return payload, _error_state(state, text, now=now, key="action:" + verb), text


def _confirm_selection(raw: str | None, action: ActionState) -> str:
    if not raw or len(raw) > MAX_DATA_LENGTH:
        raise ContractError("invalid_input", "Rofi did not provide confirmation metadata")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ContractError("invalid_input", "Rofi confirmation metadata is invalid") from error
    if not isinstance(value, Mapping):
        raise ContractError("invalid_input", "Rofi confirmation metadata is invalid")
    if value.get("type") == "cancel" and set(value) == {"type"}:
        return "cancel"
    if value.get("type") != "kill" or action.selection is None:
        raise ContractError("invalid_input", "Rofi confirmation selection is invalid")
    candidate = _action_selection(value.get("selection"))
    if candidate != dict(action.selection):
        raise ContractError("invalid_input", "Rofi confirmation selection changed")
    return "kill"


def run_rofi(
    environ: Mapping[str, str] | None = None,
    *,
    model_service: PickerModelService | None = None,
    lifecycle_service: LifecycleService | None = None,
    config: Config | None = None,
) -> int:
    """Process one Rofi script-mode invocation."""

    environ = os.environ if environ is None else environ
    try:
        retv = int(environ.get("ROFI_RETV", "0") or "0")
    except (TypeError, ValueError):
        retv = 0
    state = _parse_continuation_state(environ.get("ROFI_DATA"))
    now = time.time()

    # Escape at either browsing root is Rofi's close signal, and must work
    # even if loading the current configuration would fail.
    if retv == ROFI_RETV_CUSTOM_6 and state.action is None and not state.navigation.nested:
        return 0
    try:
        selected_config = config or load_config()
        model_service = model_service or PickerModelService(selected_config)
        lifecycle_service = lifecycle_service or LifecycleService(selected_config)
    except Exception as error:  # noqa: BLE001 - visible Rofi boundary
        print(
            _render_state(
                None,
                _error_state(
                    state, f"Configuration failed: {clean_message(error)}", now=now, key="config"
                ),
                preserve=True,
                now=now,
            ),
            end="",
        )
        return 0
    assert model_service is not None
    assert lifecycle_service is not None

    if state.blocked_action and retv != ROFI_RETV_CUSTOM_6:
        # A present action which no longer validates must never turn into a
        # browse callback. Keep it blocked until Escape/Ctrl+G cancels it.
        text = "Pending action state is invalid; press Escape to cancel"
        print(
            _render_state(
                None,
                _error_state(state, text, now=now, key="invalid-action"),
                message=text,
                preserve=True,
                now=now,
            ),
            end="",
        )
        return 0

    if retv == ROFI_RETV_CUSTOM_19:
        print(_auto_refresh_callback(model_service, state, now=now), end="")
        return 0

    if retv == ROFI_RETV_CUSTOM_6:
        try:
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
        except Exception as error:  # noqa: BLE001 - preserve a visible callback error
            print(
                _render_state(
                    None,
                    _error_state(state, clean_message(error), now=now, key="callback"),
                    preserve=True,
                    now=now,
                ),
                end="",
            )
            return 0
        next_state = (
            replace(observed, navigation=state.action.origin, action=None, blocked_action=False)
            if state.action is not None
            else replace(
                observed,
                navigation=state.navigation.root(),
                action=None,
                blocked_action=False,
            )
        )
        print(_render_state(payload, next_state, now=now), end="")
        return 0

    if retv in {ROFI_RETV_CUSTOM_2, ROFI_RETV_CUSTOM_3}:
        if state.action is not None:
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - no-op action must remain safe
                payload, observed = None, state
            print(_render_state(payload, observed, preserve=True, now=now), end="")
            return 0
        try:
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
        except Exception as error:  # noqa: BLE001 - preserve visible callback error
            print(
                _render_state(
                    None,
                    _error_state(state, clean_message(error), now=now, key="callback"),
                    preserve=True,
                    now=now,
                ),
                end="",
            )
            return 0
        next_state = replace(
            observed,
            navigation=_root_cycle(state.navigation, 1 if retv == ROFI_RETV_CUSTOM_2 else -1),
        )
        print(_render_state(payload, next_state, now=now), end="")
        return 0

    if retv in {ROFI_RETV_CUSTOM_4, ROFI_RETV_DELETE_ENTRY}:
        if state.action is not None:
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - no-op action must remain safe
                payload, observed = None, state
            print(_render_state(payload, observed, preserve=True, now=now), end="")
            return 0
        try:
            kind, selected = _parse_row_selection(environ.get("ROFI_INFO"))
            if kind != "session":
                raise ContractError("invalid_input", "selected row is not a session")
            selection = _action_selection(selected)
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
            _require_action_snapshot(payload, selection)
            action = _new_action(
                "rename" if retv == ROFI_RETV_CUSTOM_4 else "confirm-kill",
                state.navigation,
                selection=selection,
            )
            print(_render_state(payload, replace(observed, action=action), now=now), end="")
        except Exception as error:  # noqa: BLE001 - invalid callbacks cannot mutate
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - preserve callback error below
                payload, observed = None, state
            print(
                _render_state(
                    payload,
                    _error_state(
                        observed, _notice(clean_message(error)), now=now, key="action-start"
                    ),
                    preserve=True,
                    now=now,
                ),
                end="",
            )
        return 0

    if retv == ROFI_RETV_SELECTED and state.action is not None:
        action = state.action
        try:
            if action.kind == "rename":
                # Plain Enter must never ambiguously select/open while editing.
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
                print(_render_state(payload, observed, preserve=True, now=now), end="")
                return 0
            if action.kind == "choose-host":
                kind, selected = _parse_row_selection(environ.get("ROFI_INFO"))
                if kind != "host":
                    raise ContractError("invalid_input", "selected row is not a host")
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
                host_id = str(selected["hostId"])
                if not any(
                    item["hostId"].casefold() == host_id.casefold()
                    for item in _host_catalog(payload)
                ):
                    raise ContractError("invalid_input", "selected host is no longer available")
                _ensure_open_or_create(payload, host_id, action.name, lifecycle_service)
                return 0
            decision = _confirm_selection(environ.get("ROFI_INFO"), action)
            if decision == "cancel":
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
                print(
                    _render_state(
                        payload,
                        replace(observed, navigation=action.origin, action=None),
                        now=now,
                    ),
                    end="",
                )
                return 0
            selection = action.selection
            assert selection is not None
            payload, _observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
            _require_action_snapshot(payload, selection)
            lifecycle_service.kill(
                str(selection["hostId"]),
                selection["meshRevision"] if isinstance(selection["meshRevision"], str) else None,
                str(selection["serverGeneration"]),
                str(selection["sessionId"]),
                int(selection["createdAt"]),
                str(selection["name"]),
            )
            payload, next_state, message = _mutation_success(
                model_service,
                state,
                selection,
                "Session killed.",
                now=now,
                fallback_payload=payload,
            )
            print(_render_state(payload, next_state, message=message, now=now), end="")
        except Exception as error:  # noqa: BLE001 - operation remains explicit and retryable
            payload, next_state, message = _action_failure(
                model_service, state, error, verb="kill session", now=now
            )
            print(
                _render_state(payload, next_state, message=message, preserve=True, now=now), end=""
            )
        return 0

    if retv == ROFI_RETV_CUSTOM_INPUT:
        if state.action is not None and state.action.kind == "rename":
            try:
                selection = state.action.selection
                assert selection is not None
                name = _typed_name(environ.get("ROFI_INPUT"))
                payload, _observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
                _require_action_snapshot(payload, selection)
                lifecycle_service.rename(
                    str(selection["hostId"]),
                    selection["meshRevision"]
                    if isinstance(selection["meshRevision"], str)
                    else None,
                    str(selection["serverGeneration"]),
                    str(selection["sessionId"]),
                    int(selection["createdAt"]),
                    str(selection["name"]),
                    name,
                )
                payload, next_state, message = _mutation_success(
                    model_service,
                    state,
                    selection,
                    "Session renamed.",
                    now=now,
                    fallback_payload=payload,
                )
                print(_render_state(payload, next_state, message=message, now=now), end="")
            except Exception as error:  # noqa: BLE001 - preserve edit state for correction/retry
                payload, next_state, message = _action_failure(
                    model_service, state, error, verb="rename session", now=now
                )
                print(
                    _render_state(payload, next_state, message=message, preserve=True, now=now),
                    end="",
                )
            return 0
        if state.action is not None:
            # Custom input is disabled for chooser/confirmation. Do not make a
            # maliciously injected callback an alternate mutation path.
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - injected custom callback stays inert
                payload, observed = None, state
            print(_render_state(payload, observed, preserve=True, now=now), end="")
            return 0
        try:
            name = _typed_name(environ.get("ROFI_INPUT"))
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
            if state.navigation.view == VIEW_HOSTS and not state.navigation.nested:
                raise ContractError("invalid_input", "enter a host before creating a session")
            if state.navigation.view == VIEW_RECENT:
                action = _new_action("choose-host", state.navigation, name=name)
                print(_render_state(payload, replace(observed, action=action), now=now), end="")
                return 0
            assert state.navigation.host_id is not None
            _ensure_open_or_create(payload, state.navigation.host_id, name, lifecycle_service)
            return 0
        except Exception as error:  # noqa: BLE001 - invalid names/lifecycle stay in browse state
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - preserve original create error
                payload, observed = None, state
            text = f"Unable to create or open session: {clean_message(error)}"
            print(
                _render_state(
                    payload,
                    _error_state(observed, text, now=now, key="create"),
                    message=text,
                    preserve=True,
                    now=now,
                ),
                end="",
            )
        return 0

    if retv == ROFI_RETV_SELECTED:
        try:
            kind, selected = _parse_row_selection(environ.get("ROFI_INFO"))
            if kind != "session":
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
                if state.navigation.view != VIEW_HOSTS or state.navigation.nested:
                    raise ContractError("invalid_input", "selected host is not available here")
                # The selected host came from typed metadata and is checked
                # against the complete current catalog before entering it.
                if not any(
                    item["hostId"].casefold() == str(selected["hostId"]).casefold()
                    for item in _host_catalog(payload)
                ):
                    raise ContractError("invalid_input", "selected host is no longer available")
                next_state = replace(
                    observed, navigation=NavigationState(VIEW_HOSTS, str(selected["hostId"]))
                )
                print(_render_state(payload, next_state, now=now), end="")
                return 0
            payload, observed, _message = _load_observed(
                model_service, state, start_refresh=False, now=now
            )
            _open_selection(selected, lifecycle_service)
            return 0
        except Exception as error:  # noqa: BLE001 - action errors keep the picker open
            try:
                payload, observed, message = _load_observed(
                    model_service, state, start_refresh=_stale(error), now=now
                )
            except Exception:  # noqa: BLE001 - preserve the action error
                payload, observed, message = None, state, ""
            error_state = _error_state(
                observed,
                f"Unable to open session: {clean_message(error)}",
                now=now,
                key="open",
            )
            print(_render_state(payload, error_state, preserve=True, now=now), end="")
        return 0

    if retv == ROFI_RETV_CUSTOM_1:
        try:
            payload = _forced_refresh(model_service)
            observed, message = _refresh_observation(payload, state, now=now)
            errors = _payload_errors(payload)
            if (
                not message
                and not observed.has_lifecycle
                and errors
                and observed.notice_key != "payload:" + errors
            ):
                observed = _error_state(observed, errors, now=now, key="payload")
                message = errors
            timeout = observed.has_lifecycle
            print(
                _render_state(
                    payload,
                    observed,
                    message=message,
                    preserve=True,
                    timeout=timeout,
                    clear_message=not bool(message),
                    now=now,
                ),
                end="",
            )
        except Exception as error:  # noqa: BLE001 - bounded refresh failure is visible
            try:
                payload, observed, _message = _load_observed(
                    model_service, state, start_refresh=False, now=now
                )
            except Exception:  # noqa: BLE001 - preserve the refresh error
                payload, observed = None, state
            print(
                _render_state(
                    payload,
                    _error_state(
                        observed, f"Refresh failed: {clean_message(error)}", now=now, key="refresh"
                    ),
                    preserve=True,
                    now=now,
                ),
                end="",
            )
        return 0

    # Initial invocation: local/cached data renders immediately.  The model
    # may request one detached remote refresh, which timeout callbacks poll.
    try:
        payload, observed, message = _load_observed(
            model_service, state, start_refresh=True, now=now
        )
    except Exception as error:  # noqa: BLE001 - initial errors are Rofi rows
        print(
            _render_state(
                None,
                _error_state(
                    state, f"Refresh failed: {clean_message(error)}", now=now, key="refresh"
                ),
                now=now,
            ),
            end="",
        )
        return 0
    timeout = observed.has_lifecycle
    print(
        render_snapshot(
            payload,
            message=message,
            state=observed,
            timeout=timeout if timeout else None,
            now=now,
        ),
        end="",
    )
    return 0
