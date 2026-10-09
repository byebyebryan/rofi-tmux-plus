"""Legacy fresh CLI delegation, accepted runtime pin and independent viewer policy."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.inventory_service import InventoryService
from rofi_tmux_plus.observer_client import client_error, observer_api


class ObserverClientTests(unittest.TestCase):
    def setUp(self):
        fixture = (
            Path(__file__).resolve().parent.parent / "contracts/tmux-session-v1/fixtures/valid"
        )
        self.payload = json.loads((fixture / "inventory-multiple-sessions.json").read_text())
        observer_api.cache_clear()
        self.addCleanup(observer_api.cache_clear)

    def test_direct_profile_and_host_selection_are_preserved_without_legacy_reads(self):
        direct = Mock()
        direct.inventory.return_value = copy.deepcopy(self.payload)
        service = InventoryService(Config(), direct_inventory=direct)
        import rofi_tmux_plus.inventory_service as facade

        self.assertFalse(hasattr(facade, "LocalLifecycle"))
        response = service.inventory(
            requested_hosts=["starship", "STARSHIP"],
            mesh_revision="sha256:" + "a" * 64,
            panes=True,
            option_names=["@agent", "@agent"],
        )
        self.assertEqual(response, self.payload)
        direct.inventory.assert_called_once_with(
            requested_hosts=["starship", "STARSHIP"],
            mesh_revision="sha256:" + "a" * 64,
            panes=True,
            option_names=("@agent",),
        )

    def test_non_ok_direct_rows_do_not_expose_historical_sessions(self):
        payload = copy.deepcopy(self.payload)
        payload["hosts"][0]["status"] = "error"
        direct = Mock()
        direct.inventory.return_value = payload
        result = InventoryService(Config(), direct_inventory=direct).inventory(
            requested_hosts=[], mesh_revision=None, panes=False, option_names=[]
        )
        self.assertEqual(result["hosts"][0]["sessions"], [])

    def test_mesh_failure_and_future_codes_keep_the_existing_public_error_contract(self):
        for code in (
            "invalid_config",
            "future_provider_failure",
            "Future.provider-error",
            "stale_mesh",
        ):
            with self.subTest(code=code):
                error = type(
                    "ProviderFailure", (Exception,), {"code": code, "message": "provider refused"}
                )()
                self.assertEqual(client_error(error).code, code)
        for code in ([], "invalid\ncode", "x" * 65):
            error = type("UnsafeFailure", (Exception,), {"code": code})()
            self.assertEqual(client_error(error).code, "operation_failed")

    def test_supported_client_errors_keep_legacy_code_and_host_without_fallback(self):
        direct = Mock()
        direct.inventory.side_effect = type(
            "ClientError",
            (Exception,),
            {"code": "unknown_host", "message": "unknown", "host_id": "x"},
        )()
        with self.assertRaises(ContractError) as caught:
            InventoryService(Config(), direct_inventory=direct).inventory(
                requested_hosts=["x"], mesh_revision=None, panes=False, option_names=[]
            )
        self.assertEqual(caught.exception.envelope()["error"]["hostId"], "x")
        self.assertEqual(caught.exception.code, "unknown_host")

    def test_viewers_use_the_single_direct_catalog_and_keep_owner_facts(self):
        snapshot = SimpleNamespace(
            local_host=SimpleNamespace(host_id="snap"), policy=SimpleNamespace(executable="ssh")
        )
        adapter = Mock()
        adapter.load.return_value = snapshot
        seen = []

        class Direct:
            def __init__(inner, *, mesh):
                inner.mesh = mesh

            def inventory(inner, **kwargs):
                inner.mesh.load(timeout_seconds=5)
                seen.append(kwargs)
                return copy.deepcopy(self.payload)

        api = SimpleNamespace(
            mesh=SimpleNamespace(HostMeshAdapter=lambda: adapter),
            direct=SimpleNamespace(DirectInventory=Direct),
            desktop_config=SimpleNamespace(DesktopConfig=Mock(return_value="owned-config")),
        )
        with (
            patch("rofi_tmux_plus.inventory_service.observer_api", return_value=api),
        ):
            result = InventoryService(Config(), mesh_adapter=adapter).inventory(
                requested_hosts=[],
                mesh_revision=None,
                panes=False,
                option_names=[],
                with_viewers=True,
            )
        self.assertEqual(result, self.payload)
        adapter.load.assert_called_once_with(timeout_seconds=5)
        self.assertTrue(seen[0]["with_viewers"])
        self.assertEqual(seen[0]["desktop_config"], "owned-config")

    def test_accepted_installed_api_imports_execute_no_native_command(self):
        with patch("subprocess.Popen", side_effect=AssertionError("native process on import")):
            api = observer_api()
        self.assertEqual(api.prepared.read_cached.__module__, "tmux_observer_client.public")
        self.assertEqual(api.direct.DirectInventory.__module__, "tmux_observer_client.direct")

    def test_runtime_byte_drift_and_missing_dependency_fail_before_native_reads(self):
        with patch("rofi_tmux_plus.observer_client.hashlib.sha256") as digest:
            digest.return_value.hexdigest.return_value = "0" * 64
            with self.assertRaisesRegex(ContractError, "bytes differ"):
                observer_api()
        with (
            patch(
                "rofi_tmux_plus.inventory_service.observer_api", side_effect=ImportError("missing")
            ),
            self.assertRaises(ContractError),
        ):
            InventoryService(Config()).inventory(
                requested_hosts=[], mesh_revision=None, panes=False, option_names=[]
            )
