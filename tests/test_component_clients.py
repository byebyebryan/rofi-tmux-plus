"""Active consumer boundaries, independent C5 binding and legacy argv parity."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rofi_tmux_plus import cli
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.lifecycle_service import ActionService, LifecycleService
from rofi_tmux_plus.observer_client import observer_api

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts/tmux-session-v1/fixtures/valid"


class ComponentClientTests(unittest.TestCase):
    def test_browse_and_lazy_action_objects_import_with_implementations_blocked(self):
        import tmux_observer

        program = """
import importlib.abc, io, os, sys
from contextlib import redirect_stdout
sys.path[:0] = sys.argv[1:3]
blocked = ('tmux_observer.collector', 'tmux_observer.attachment_collector', 'tmux_observer.owner',
           'tmux_observer_client.direct', 'tmux_observer_client.mesh', 'tmux_observer_client.ssh',
           'tmux_observer_client.fleet', 'tmux_observer_client._desktop_scan',
           'tmux_observer_actions.public', 'tmux_observer_actions.lifecycle', 'tmux_observer_actions.tmux')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *_args):
        if fullname in blocked:
            raise ImportError('implementation imported by browse: ' + fullname)
sys.meta_path.insert(0, Block())
os.environ['XDG_RUNTIME_DIR'] = sys.argv[3]
os.environ['XDG_CACHE_HOME'] = sys.argv[3] + '/cache'
os.environ['XDG_STATE_HOME'] = sys.argv[3] + '/state'
from rofi_tmux_plus import cli, launcher, picker_watch, rofi
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.lifecycle_service import ActionService, LifecycleService
from rofi_tmux_plus.observer_client import observer_api
ActionService(Config()); LifecycleService(Config())
assert observer_api().prepared.read_cached
with redirect_stdout(io.StringIO()):
    assert rofi.run_rofi({'ROFI_RETV': '0'}, config=Config()) == 0
assert not set(blocked).intersection(sys.modules)
"""
        with tempfile.TemporaryDirectory(prefix="tmux-plus-imports-") as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    program,
                    str(ROOT),
                    str(Path(tmux_observer.__file__).parents[1]),
                    temporary,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def action_api(self, operation="open", *, failure=False, retarget=False):
        api = observer_api()
        fixture = json.loads(
            (
                FIXTURES / ("open-success.json" if operation == "open" else "kill-success.json")
            ).read_text()
        )
        sdk = Mock()

        def execute(request):
            legacy = copy.deepcopy(fixture)
            reference = copy.deepcopy(request["sessionRef"])
            if retarget:
                reference["createdAt"] += 1
            if operation == "open":
                legacy["session"].update(reference)
            else:
                legacy["reference"] = reference
            outcome = {
                "nativeEffect": "none" if operation == "open" else "confirmed",
                "terminalSpawn": "not_requested",
                "attachment": "not_requested",
                "focus": "confirmed" if operation == "open" else "not_requested",
                "viewerClose": "not_requested",
                "transport": "local",
            }
            if failure:
                outcome.update(
                    nativeEffect="uncertain", focus="not_requested", transport="uncertain"
                )
            return {
                "protocol": "tmux-observer.action.v1",
                "schemaVersion": 1,
                "requestId": request["requestId"],
                "operation": operation,
                "hostId": request["hostId"],
                "meshRevision": request["meshRevision"],
                "sessionRef": reference,
                "ok": not failure,
                "outcome": outcome,
                "legacyResult": None if failure else legacy,
                "error": {
                    "code": "action_uncertain",
                    "message": "Remote write acknowledgement was lost",
                }
                if failure
                else None,
                "automaticRetry": False,
            }

        sdk.execute.side_effect = execute
        factory = Mock(return_value=sdk)
        return (
            SimpleNamespace(
                action_contract=api.action_contract,
                actions=SimpleNamespace(
                    ActionClient=factory, ActionConfig=api.actions.ActionConfig
                ),
            ),
            sdk,
            factory,
        )

    def test_picker_open_passes_canonical_full_reference_and_unique_reuse_intent(self):
        api, sdk, factory = self.action_api()
        with patch("rofi_tmux_plus.lifecycle_service.observer_api", return_value=api):
            client = ActionService(Config(terminal=("kitty",)))
            factory.assert_not_called()
            result = client.open("local", None, "tmux-v1:10:20:/tmp/default", "$6", 30)
        request = sdk.execute.call_args.args[0]
        self.assertEqual(
            request["sessionRef"],
            {
                "hostId": "local",
                "serverGeneration": "tmux-v1:10:20:/tmp/default",
                "sessionId": "$6",
                "createdAt": 30,
            },
        )
        self.assertEqual(request["parameters"], {"viewerPolicy": "reuse_unique"})
        self.assertTrue(result["focused"])
        self.assertFalse(result["terminalLaunched"])

    def test_picker_kill_keeps_frozen_name_and_does_not_retry_uncertain_result(self):
        api, sdk, _factory = self.action_api("kill", failure=True)
        with (
            patch("rofi_tmux_plus.lifecycle_service.observer_api", return_value=api),
            self.assertRaises(ContractError) as error,
        ):
            ActionService(Config()).kill(
                "local", None, "tmux-v1:10:20:/tmp/default", "$6", 30, "frozen-name"
            )
        self.assertEqual(error.exception.code, "action_uncertain")
        self.assertEqual(sdk.execute.call_count, 1)
        self.assertEqual(sdk.execute.call_args.args[0]["guards"]["expectedName"], "frozen-name")

    def test_retargeted_sdk_result_is_rejected_by_consumer(self):
        api, sdk, _factory = self.action_api(retarget=True)
        with (
            patch("rofi_tmux_plus.lifecycle_service.observer_api", return_value=api),
            self.assertRaises(ContractError),
        ):
            ActionService(Config()).open("local", None, "tmux-v1:10:20:/tmp/default", "$6", 30)
        self.assertEqual(sdk.execute.call_count, 1)

    def test_legacy_facade_keeps_argv_limits_and_guard_shape_without_c5_reencoding(self):
        target = Mock()
        factory = Mock(return_value=target)
        actual = observer_api().actions
        api = SimpleNamespace(
            actions=SimpleNamespace(ActionConfig=actual.ActionConfig, LifecycleService=factory)
        )
        with patch("rofi_tmux_plus.lifecycle_service.observer_api", return_value=api):
            client = LifecycleService(Config())
            factory.assert_not_called()
            arguments = ("local", None, "name", "/tmp", (), ("true",) * 129, False, None, False)
            client.create(*arguments)
        target.create.assert_called_once_with(*arguments)

    def test_all_old_public_success_and_error_fixtures_remain_representable(self):
        for path in sorted(FIXTURES.glob("*.json")):
            value = json.loads(path.read_text())
            name = path.name
            operation = name.split("-", 1)[0]
            if name.startswith("close-viewer"):
                operation = "close-viewer"
            if operation == "error":
                continue
            with self.subTest(fixture=name):
                cli._validate_public_result(operation, value)
        for path in sorted(FIXTURES.glob("error-*.json")):
            value = json.loads(path.read_text())
            error = value["error"]
            with self.subTest(fixture=path.name):
                result = ContractError(
                    error["code"], error["message"], error.get("hostId")
                ).envelope()
                self.assertEqual(result, value)


if __name__ == "__main__":
    unittest.main()
