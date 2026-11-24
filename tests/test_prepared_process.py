"""A selected Observer executable provides prepared data without Python imports."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_picker_watch import frame_fixture

from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.picker_notify import read_notification
from rofi_tmux_plus.picker_watch import OwnedWatch
from rofi_tmux_plus.prepared_model import PreparedModelService
from rofi_tmux_plus.prepared_process import CLIENT_ENV, PreparedProcess


class PreparedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.executable = self.root / "observer-client"
        self.log = self.root / "arguments"
        self.frame = frame_fixture()
        (self.root / "frame.json").write_text(json.dumps(self.frame), encoding="utf-8")
        self.environment = {**os.environ, CLIENT_ENV: str(self.executable)}
        for module in ("prepared_model", "picker_watch"):
            self.enterContext(
                patch(
                    f"rofi_tmux_plus.{module}.observer_api",
                    side_effect=AssertionError("loaded the pinned Observer"),
                )
            )

    def executable_script(self, body):
        source = f"#!{sys.executable}\n" + body
        self.executable.write_text(source, encoding="utf-8")
        self.executable.chmod(0o700)

    def compatible_executable(self):
        self.executable_script(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "root = Path(__file__).parent\n"
            "frame = json.loads((root / 'frame.json').read_text())\n"
            "assert 'PYTHONPATH' not in os.environ and 'PYTHONHOME' not in os.environ\n"
            "arguments = sys.argv[1:]\n"
            "with (root / 'arguments').open('a') as log:\n"
            "    log.write(json.dumps(arguments) + '\\n')\n"
            "if arguments[0] == 'context':\n"
            "    print(frame['contextId'])\n"
            "else:\n"
            "    frame['kind'] = 'resync' if arguments[0] == 'watch' else 'view'\n"
            "    print(json.dumps(frame), flush=True)\n"
            "    if frame['kind'] == 'resync':\n"
            "        time.sleep(10)\n"
        )

    def test_snapshot_and_watch_use_selected_cli_without_pinned_observer(self):
        self.compatible_executable()
        environment = {**self.environment, "PYTHONPATH": "/unrelated", "PYTHONHOME": "/unrelated"}
        service = PreparedModelService(environ=environment)
        self.assertEqual(service.context, self.frame["contextId"])
        service.load()
        with OwnedWatch(self.root, service.context, environment) as watch:
            expires = time.monotonic() + 2
            while time.monotonic() < expires and not read_notification(self.root)["ready"]:
                time.sleep(0.01)
            self.assertTrue(read_notification(self.root)["ready"])
            child = watch.child
        self.assertIsNotNone(child.poll())
        self.assertFalse(read_notification(self.root)["ready"])
        self.assertEqual(
            [json.loads(line) for line in self.log.read_text().splitlines()],
            [
                ["context"],
                ["snapshot", "--json", "--context-id", service.context, "--access", "cached"],
                ["watch", "--json", "--context-id", service.context],
            ],
        )

    def test_explicit_invalid_selection_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "absolute executable"):
            PreparedModelService(environ={**self.environment, CLIENT_ENV: "tmux-observer-client"})
        with self.assertRaises(FileNotFoundError):
            PreparedModelService(environ=self.environment)

    def test_foreign_frame_is_unavailable(self):
        self.compatible_executable()
        service = PreparedModelService(environ=self.environment)
        self.frame["contextId"] = "f" * 32
        (self.root / "frame.json").write_text(json.dumps(self.frame), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "different operation or scope"):
            service.load()

    def test_process_output_is_bounded(self):
        self.executable_script("print('a' * 200, flush=True)\n")
        with self.assertRaisesRegex(ValueError, "response exceeded"):
            _context = PreparedProcess(self.environment).context
