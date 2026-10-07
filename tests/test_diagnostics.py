from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rofi_tmux_plus.diagnostics import TIMING_ENV, record, timed


class TimingDiagnosticsTests(unittest.TestCase):
    def test_opt_in_records_counts_and_duration_without_output_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"

            @timed("remote_command", process_result=True)
            def operation():
                return subprocess.CompletedProcess([], 0, "private output", "private diagnostic")

            with patch.dict(os.environ, {TIMING_ENV: str(path)}):
                result = operation()
            self.assertEqual(result.stdout, "private output")
            event = json.loads(path.read_text())
            self.assertEqual(event["stdoutBytes"], 14)
            self.assertGreaterEqual(event["durationMs"], 0)
            self.assertNotIn("private", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsafe_sink_is_inert_and_never_blocks_or_changes_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.write_text("preserve")
            sink = base / "sink"
            sink.symlink_to(target)
            with patch.dict(os.environ, {TIMING_ENV: str(sink)}):
                record("test", time.monotonic())
            self.assertEqual(target.read_text(), "preserve")
            sink.unlink()
            os.mkfifo(sink)
            with patch.dict(os.environ, {TIMING_ENV: str(sink)}):
                record("test", time.monotonic())
