"""Verify packaged mode payloads before handing compatibility checks to Rofi."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
from pathlib import Path

from .errors import ContractError


def file_bytes(path, *, limit):
    fd = os.open(Path(path).resolve(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("native integration file is not regular and bounded")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("native integration file exceeded its bound")
        return raw
    finally:
        os.close(fd)


def verify_native(*, directory=None, executable=None):
    root = Path(__file__).with_name("native") if directory is None else Path(directory)
    binary = shutil.which("rofi") if executable is None else executable
    try:
        record = json.loads(file_bytes(root / "native-artifact.json", limit=16384))
        if (
            record["schemaVersion"] != 1
            or record["modeAbi"] != 7
            or record["architecture"] != platform.machine()
            or set(record["files"]) != {"tmux-plus.so", "tmux-plus-notify.c", "rofi-tmux-plus-mode"}
            or {path.name for path in root.glob("*.so")} != {"tmux-plus.so"}
        ):
            raise ValueError("unsupported native integration descriptor")
        if binary is None:
            raise ValueError("Rofi executable was not found")
        binary = Path(binary).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Rofi executable is not a regular executable file")
        for name, expected in record["files"].items():
            if hashlib.sha256(file_bytes(root / name, limit=1024 * 1024)).hexdigest() != expected:
                raise ValueError("native integration bytes differ from their descriptor")
        if not os.access(root / "rofi-tmux-plus-mode", os.X_OK):
            raise ValueError("native script helper is not executable")
        # The recorded Rofi digest is build provenance. Rofi's loader checks
        # Mode ABI, and the module checks its refresh capabilities at init.
        return str(binary), root.resolve()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ContractError(
            "operation_failed", "Tmux Plus native mode unavailable: " + str(error)
        ) from error
