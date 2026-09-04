"""Structured contract failures and deliberately bounded diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_message(value: object, *, limit: int = 480) -> str:
    """Make process diagnostics safe to include in one-line JSON responses."""
    text = _CONTROL.sub(" ", str(value)).strip()
    text = " ".join(text.split())
    return text[:limit] or "operation failed"


@dataclass(slots=True)
class ContractError(Exception):
    code: str
    message: str
    host_id: str | None = None

    def __post_init__(self) -> None:
        self.message = clean_message(self.message)

    def envelope(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.host_id is not None:
            error["hostId"] = self.host_id
        return {"schemaVersion": 1, "ok": False, "error": error}


class TmuxMissing(ContractError):
    def __init__(self, message: str = "tmux is not available") -> None:
        super().__init__("tmux_missing", message)


class NoServer(ContractError):
    def __init__(self) -> None:
        super().__init__("session_not_found", "no default tmux server is running")
