"""Typed JSON shapes for Tmux Session Contract v1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionReference:
    host_id: str
    server_generation: str
    session_id: str
    created_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "hostId": self.host_id,
            "serverGeneration": self.server_generation,
            "sessionId": self.session_id,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class Pane:
    pane_id: str
    pid: int | None
    current_path: str | None
    current_command: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "paneId": self.pane_id,
            "pid": self.pid,
            "currentPath": self.current_path,
            "currentCommand": self.current_command,
        }


@dataclass(frozen=True, slots=True)
class Session:
    reference: SessionReference
    name: str | None
    activity_at: int | None
    last_attached_at: int | None
    attached_clients: int | None
    pending: bool
    window_count: int | None
    session_path: str | None
    current_window: str | None
    current_path: str | None
    panes: tuple[Pane, ...] | None = None
    options: dict[str, str | None] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            **self.reference.as_dict(),
            "name": self.name,
            "activityAt": self.activity_at,
            "lastAttachedAt": self.last_attached_at,
            "attachedClients": self.attached_clients,
            "pending": self.pending,
            "windowCount": self.window_count,
            "sessionPath": self.session_path,
            "currentWindow": self.current_window,
            "currentPath": self.current_path,
        }
        if self.panes is not None:
            result["panes"] = [pane.as_dict() for pane in self.panes]
        if self.options is not None:
            result["options"] = self.options
        return result
