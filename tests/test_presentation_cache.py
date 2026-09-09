from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rofi_tmux_plus.presentation_cache import (
    MAX_SNAPSHOT_BYTES,
    SNAPSHOT_RETENTION_COUNT,
    PresentationSnapshotCache,
    SnapshotCacheError,
    valid_snapshot_key,
)


class PresentationSnapshotCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = PresentationSnapshotCache(Path(self.directory.name))

    @staticmethod
    def value(number: int) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "generatedAt": number,
            "hosts": [],
        }

    def snapshot_files(self) -> list[Path]:
        return sorted(Path(self.directory.name).glob("*.json"))

    def test_round_trip_is_content_addressed_and_private(self) -> None:
        value = {"hosts": [], "generatedAt": 1, "schemaVersion": 1}
        key = self.cache.store(value)
        path = Path(self.directory.name) / f"{key}.json"

        self.assertTrue(valid_snapshot_key(key))
        self.assertEqual(value, self.cache.load(key))
        self.assertEqual(key, self.cache.store(dict(reversed(tuple(value.items())))))
        self.assertEqual(0o700, stat.S_IMODE(Path(self.directory.name).stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([], list(Path(self.directory.name).glob(".snapshot-*.tmp")))

    def test_prune_retains_newest_bounded_set_and_current_key(self) -> None:
        keys: list[str] = []
        for number in range(SNAPSHOT_RETENTION_COUNT + 1):
            key = self.cache.store(self.value(number))
            keys.append(key)
            # Make the ordering deterministic even on filesystems with coarse
            # timestamp resolution. The newest store still receives wall time
            # and is protected explicitly by store().
            if number < SNAPSHOT_RETENTION_COUNT:
                os.utime(Path(self.directory.name) / f"{key}.json", (number + 1, number + 1))

        self.assertEqual(SNAPSHOT_RETENTION_COUNT, len(self.snapshot_files()))
        self.assertIsNone(self.cache.load(keys[0]))
        self.assertEqual(self.value(SNAPSHOT_RETENTION_COUNT), self.cache.load(keys[-1]))

    def test_prune_leaves_non_snapshot_and_unsafe_entries_untouched(self) -> None:
        directory = Path(self.directory.name)
        unrelated = directory / "notes.json"
        unrelated.write_text("keep", encoding="utf-8")
        symlink_target = directory / "target"
        symlink_target.write_text("keep", encoding="utf-8")
        symlink = directory / ("b" * 64 + ".json")
        symlink.symlink_to(symlink_target.name)
        unsafe = directory / ("c" * 64 + ".json")
        unsafe.write_text("keep", encoding="utf-8")
        unsafe.chmod(0o644)

        removed = self.cache.prune()

        self.assertEqual(0, removed)
        self.assertTrue(unrelated.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(unsafe.exists())
        self.assertIsNone(self.cache.load("b" * 64))
        self.assertIsNone(self.cache.load("c" * 64))

    def test_invalid_or_corrupt_snapshot_is_rejected(self) -> None:
        key = self.cache.store(self.value(1))
        path = Path(self.directory.name) / f"{key}.json"

        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(self.cache.load(key))
        path.write_text(
            json.dumps(
                {
                    "payload": self.value(2),
                    "schemaVersion": 1,
                    "snapshotKey": key,
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(self.cache.load(key))
        for candidate in ("", "../" + key, "A" * 64, "x" * 63, None, 1):
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.cache.load(candidate))

        oversized = Path(self.directory.name) / ("d" * 64 + ".json")
        oversized.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))
        oversized.chmod(0o600)
        self.assertIsNone(self.cache.load("d" * 64))

    def test_unsupported_or_oversized_payload_is_not_stored(self) -> None:
        with self.assertRaises(SnapshotCacheError):
            self.cache.store({"schemaVersion": 2})
        with self.assertRaises(SnapshotCacheError):
            self.cache.store(
                {
                    "schemaVersion": 1,
                    "large": "x" * MAX_SNAPSHOT_BYTES,
                }
            )
        self.assertEqual([], self.snapshot_files())


if __name__ == "__main__":
    unittest.main()
