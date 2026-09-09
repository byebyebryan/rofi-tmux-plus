"""Private content-addressed snapshots for cache-only Rofi callbacks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
SNAPSHOT_KEY_LENGTH = hashlib.sha256().digest_size * 2
SNAPSHOT_RETENTION_COUNT = 256
_SNAPSHOT_KEY_CHARS = frozenset("0123456789abcdef")
_SNAPSHOT_FILENAME = re.compile(rf"[0-9a-f]{{{SNAPSHOT_KEY_LENGTH}}}\.json\Z")


class SnapshotCacheError(Exception):
    """Raised when a model payload cannot be safely persisted."""


def cache_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Return the private presentation-cache directory for this user."""

    env = os.environ if environ is None else environ
    configured = env.get("XDG_CACHE_HOME")
    base = Path(configured) if configured else Path.home() / ".cache"
    if not base.is_absolute():
        base = Path.home() / ".cache"
    return base / "rofi-tmux-plus" / "presentation-v1"


def valid_snapshot_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SNAPSHOT_KEY_LENGTH
        and all(character in _SNAPSHOT_KEY_CHARS for character in value)
    )


def _canonical_payload(payload: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    if payload.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotCacheError("picker model payload has an unsupported schema")
    value = dict(payload)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SnapshotCacheError("picker model payload is not valid JSON") from error
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise SnapshotCacheError("picker model payload is too large")
    return value, encoded


def _owned_directory(path: Path, *, create: bool) -> bool:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid != os.getuid():
        return False
    if stat.S_IMODE(info.st_mode) != 0o700:
        if not create:
            return False
        try:
            path.chmod(0o700)
            info = path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            return False
    return True


def _owned_directory_fd(path: Path) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(descriptor)
        return None
    return descriptor


def _owned_regular(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    )


class PresentationSnapshotCache:
    """Persist and load exact model payloads without following untrusted paths."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.directory = cache_directory(environ) if directory is None else Path(directory)

    def _path(self, key: str) -> Path | None:
        if not valid_snapshot_key(key):
            return None
        return self.directory / (key + ".json")

    def store(self, payload: Mapping[str, object]) -> str:
        """Atomically store one bounded payload and return its content key."""

        value, payload_bytes = _canonical_payload(payload)
        key = hashlib.sha256(payload_bytes).hexdigest()
        envelope = {
            "payload": value,
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "snapshotKey": key,
        }
        try:
            encoded = json.dumps(
                envelope,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise SnapshotCacheError("picker model snapshot is not valid JSON") from error
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise SnapshotCacheError("picker model snapshot is too large")
        if not _owned_directory(self.directory, create=True):
            raise SnapshotCacheError("picker model snapshot cache is not private")
        target = self._path(key)
        assert target is not None
        descriptor, temporary = tempfile.mkstemp(
            prefix=".snapshot-", suffix=".tmp", dir=self.directory
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = ""
            try:
                directory_descriptor = os.open(
                    self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
            except OSError:
                pass
            else:
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except OSError as error:
            raise SnapshotCacheError("picker model snapshot cache write failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.prune(keep_key=key)
        return key

    def prune(self, *, keep_key: str | None = None) -> int:
        """Remove old owned snapshots while retaining the newest bounded set.

        Only the cache's exact content-addressed filename shape is considered.
        Files with unsafe permissions, a different owner, or a non-regular type
        are left untouched rather than being interpreted or removed. The
        optional current key remains protected if timestamp ordering is tied.
        """

        directory_descriptor = _owned_directory_fd(self.directory)
        if directory_descriptor is None:
            return 0
        try:
            entries = tuple(os.listdir(directory_descriptor))
        except OSError:
            os.close(directory_descriptor)
            return 0
        try:
            candidates: list[tuple[Path, int]] = []
            for name in entries:
                if _SNAPSHOT_FILENAME.fullmatch(name) is None:
                    continue
                try:
                    info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                except OSError:
                    continue
                if not _owned_regular(info):
                    continue
                candidates.append((Path(name), info.st_mtime_ns))
            candidates.sort(key=lambda item: (-item[1], item[0].name))
            protected = {keep_key} if valid_snapshot_key(keep_key) else set()
            retained = {path for path, _mtime in candidates[:SNAPSHOT_RETENTION_COUNT]}
            retained.update(path for path, _mtime in candidates if path.stem in protected)
            removed = 0
            for path, _mtime in candidates:
                if path in retained:
                    continue
                try:
                    os.unlink(path.name, dir_fd=directory_descriptor)
                except OSError:
                    continue
                removed += 1
            return removed
        finally:
            os.close(directory_descriptor)

    def load(self, key: object) -> dict[str, object] | None:
        """Load only the exact validated content-addressed snapshot key."""

        if not isinstance(key, str):
            return None
        path = self._path(key)
        if path is None:
            return None
        directory_descriptor = _owned_directory_fd(self.directory)
        if directory_descriptor is None:
            return None
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
            if not _owned_regular(os.fstat(descriptor)):
                return None
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_descriptor)
        if not raw or len(raw) > MAX_SNAPSHOT_BYTES:
            return None
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"payload", "schemaVersion", "snapshotKey"}
            or envelope.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION
            or envelope.get("snapshotKey") != key
            or not isinstance(envelope.get("payload"), dict)
        ):
            return None
        payload = envelope["payload"]
        try:
            value, encoded = _canonical_payload(payload)
        except SnapshotCacheError:
            return None
        if hashlib.sha256(encoded).hexdigest() != key:
            return None
        return value
