"""Accepted Observer imports and public client error translation."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from .errors import ContractError, clean_message


@lru_cache(maxsize=1)
def observer_api() -> SimpleNamespace:
    """Require the accepted runtime bytes; never substitute a native read path."""
    try:
        pin = json.loads(Path(__file__).with_name("observer-artifact.json").read_text())
        try:
            owner = importlib.import_module("tmux_observer")
        except ModuleNotFoundError:
            # The managed frozen-wheel bundle exposes one fixed Python root.
            root = Path.home() / ".local/share/tmux-observer/python"
            if not root.is_dir():
                raise
            sys.path.insert(0, str(root))
            owner = importlib.import_module("tmux_observer")
        client = importlib.import_module("tmux_observer_client")
        if owner.__version__ != pin["version"]:
            raise ValueError("Observer version differs from the accepted client artifact")
        roots = {
            "tmux_observer": Path(owner.__file__).resolve().parent,
            "tmux_observer_client": Path(client.__file__).resolve().parent,
        }
        if len({root.parent for root in roots.values()}) != 1:
            raise ValueError("Observer packages have different installation roots")
        actual = {
            package + "/" + path.relative_to(root).as_posix()
            for package, root in roots.items()
            for path in root.rglob("*.py")
        }
        if actual != set(pin["modules"]):
            raise ValueError("Observer runtime module coverage differs from the accepted artifact")
        for name, digest in pin["modules"].items():
            package, relative = name.split("/", 1)
            if hashlib.sha256((roots[package] / relative).read_bytes()).hexdigest() != digest:
                raise ValueError("Observer runtime bytes differ from the accepted artifact")
        return SimpleNamespace(
            protocol=importlib.import_module("tmux_observer.public"),
            prepared=importlib.import_module("tmux_observer_client.public"),
            direct=importlib.import_module("tmux_observer_client.direct"),
            mesh=importlib.import_module("tmux_observer_client.mesh"),
        )
    except (ImportError, OSError, ValueError, AttributeError) as error:
        raise ContractError(
            "operation_failed", "Accepted Tmux Observer is unavailable: " + clean_message(error)
        ) from error


def client_error(error: Exception) -> ContractError:
    if isinstance(error, ContractError):
        return error
    code = getattr(error, "code", "operation_failed")
    if code not in {
        "invalid_input",
        "unknown_host",
        "stale_mesh",
        "host_unreachable",
        "tmux_missing",
    }:
        code = "operation_failed"
    host = getattr(error, "host_id", None)
    return ContractError(code, getattr(error, "message", clean_message(error)), host)


class MeshCapture:
    """Retain the direct operation's one catalog for optional viewer policy."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.snapshot = None

    def load(self, **kwargs):
        self.snapshot = self.adapter.load(**kwargs)
        return self.snapshot

    def report_route(self, **kwargs):
        return self.adapter.report_route(**kwargs)
