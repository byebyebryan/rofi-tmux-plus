"""Bounded regular files in a launcher-owned, private per-picker directory."""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def runtime_root(path: str | Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("picker runtime must be an owned private directory")
        yield fd
    finally:
        os.close(fd)


def read_private(path: str | Path, name: str, *, limit: int) -> bytes | None:
    if not name or Path(name).name != name:
        raise ValueError("invalid picker runtime filename")
    with runtime_root(path) as root:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > limit
            ):
                raise ValueError("picker runtime file is not owned, regular and bounded")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                value = stream.read(limit + 1)
            if len(value) > limit:
                raise ValueError("picker runtime file exceeded its bound")
            return value
        finally:
            os.close(fd)


def write_private(path: str | Path, name: str, value: bytes, *, limit: int) -> None:
    if not name or Path(name).name != name or len(value) > limit:
        raise ValueError("invalid or oversized picker runtime write")
    with runtime_root(path) as root:
        temporary = "." + name + "." + uuid.uuid4().hex
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
            os.replace(temporary, name, src_dir_fd=root, dst_dir_fd=root)
        finally:
            try:
                os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass
