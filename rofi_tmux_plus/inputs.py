"""Pure legacy CLI token checks; no native or action imports."""

import re
from collections.abc import Iterable

from .config import require_clean_text
from .errors import ContractError

_SESSION_ID = re.compile(r"^\$[0-9]+$")
_USER_OPTION = re.compile(r"^@[A-Za-z0-9_.-]+$")


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
