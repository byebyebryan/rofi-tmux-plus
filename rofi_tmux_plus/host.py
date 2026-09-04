"""A local-only Host Mesh-compatible identity without SSH Plus imports."""

from __future__ import annotations

import re
import socket
import unicodedata
from dataclasses import dataclass

from .errors import ContractError

_HOST_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)
_NATIVE_LIMIT = 255


def _is_safe_identifier(value: str) -> bool:
    return bool(_HOST_TOKEN.fullmatch(value)) and not any(
        unicodedata.category(char).startswith("C") for char in value
    )


def _is_safe_alias(value: str) -> bool:
    return (
        bool(value)
        and value.strip() == value
        and not value.startswith("-")
        and not any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    )


def _native_hostname(value: str) -> str:
    """Keep host output useful without leaking controls into JSON consumers."""
    sanitized = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in value
    )
    sanitized = " ".join(sanitized.split())[:_NATIVE_LIMIT]
    return sanitized or "localhost"


def _display_hostname(value: str, fallback: str) -> str:
    if (
        value
        and value.strip() == value
        and not any(unicodedata.category(char).startswith("C") for char in value)
    ):
        return value[:_NATIVE_LIMIT]
    return fallback


@dataclass(frozen=True, slots=True)
class LocalHost:
    host_id: str
    display: str
    native_hostname: str
    aliases: frozenset[str]


def local_host(hostname: str | None = None) -> LocalHost:
    short_source = hostname if hostname is not None else socket.gethostname()
    raw_short = short_source.split(".", 1)[0] or "localhost"
    raw_full = hostname if hostname is not None else socket.getfqdn() or raw_short
    host_id = raw_short.casefold() if _is_safe_identifier(raw_short) else "localhost"
    aliases = [candidate for candidate in (raw_full, raw_short) if _is_safe_alias(candidate)]
    if not aliases:
        aliases = [host_id]
    return LocalHost(
        host_id,
        _display_hostname(raw_short, host_id),
        _native_hostname(raw_full),
        frozenset(aliases),
    )


def resolve_local_host(host_id: str | None, host: LocalHost | None = None) -> LocalHost:
    selected = local_host() if host is None else host
    if host_id is None or any(host_id.casefold() == alias.casefold() for alias in selected.aliases):
        return selected
    raise ContractError("unknown_host", f"unknown local host: {host_id}", host_id)
