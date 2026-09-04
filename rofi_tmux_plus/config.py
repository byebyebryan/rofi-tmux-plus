"""Strict, deliberately small version-1 configuration parsing."""

from __future__ import annotations

import os
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .errors import ContractError


def has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def require_clean_text(value: str, field: str) -> str:
    if "\x00" in value or has_control(value):
        raise ContractError("invalid_input", f"{field} must not contain NUL or control characters")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    terminal: tuple[str, ...] = ("ghostty",)
    refresh_seconds: int = 30
    attach_timeout_seconds: int = 60


def config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "rofi-tmux-plus" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    selected = config_path() if path is None else path
    if not selected.exists():
        return Config()
    try:
        with selected.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            "invalid_input", f"invalid rofi-tmux-plus configuration: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise ContractError("invalid_input", "rofi-tmux-plus configuration must be a TOML table")
    expected = {"schema_version", "terminal", "refresh_seconds", "attach_timeout_seconds"}
    unknown = sorted(set(raw) - expected)
    if unknown:
        raise ContractError("invalid_input", f"unknown configuration key: {unknown[0]}")
    if raw.get("schema_version") != 1:
        raise ContractError("invalid_input", "configuration schema_version must be 1")
    defaults = Config()
    terminal = raw.get("terminal", list(defaults.terminal))
    if (
        not isinstance(terminal, list)
        or not terminal
        or any(not isinstance(item, str) or not item or has_control(item) for item in terminal)
    ):
        raise ContractError(
            "invalid_input", "terminal must be a nonempty argv array without control characters"
        )
    refresh = raw.get("refresh_seconds", defaults.refresh_seconds)
    attach = raw.get("attach_timeout_seconds", defaults.attach_timeout_seconds)
    if isinstance(refresh, bool) or not isinstance(refresh, int) or not 1 <= refresh <= 86400:
        raise ContractError(
            "invalid_input", "refresh_seconds must be an integer from 1 through 86400"
        )
    if isinstance(attach, bool) or not isinstance(attach, int) or not 1 <= attach <= 3600:
        raise ContractError(
            "invalid_input", "attach_timeout_seconds must be an integer from 1 through 3600"
        )
    return Config(tuple(terminal), refresh, attach)
