"""Consumer display leases remain distinct from fresh desktop/action evidence."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rofi_tmux_plus import rofi
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.observer_client import observer_api
from rofi_tmux_plus.picker_notify import write_notification
from rofi_tmux_plus.picker_watch import notification_material
from rofi_tmux_plus.prepared_model import apply_expiry, project_frame


def retained_frame():
    frame = json.loads((Path(__file__).parent / "fixtures/prepared-fleet-frame.json").read_text())
    view = frame["snapshot"]
    host = view["hosts"][0]
    session = host["sessions"][0]
    session["attachedClients"] = 1
    host["localBindings"] = {
        "protocol": "tmux-observer.bindings.v1",
        "schemaVersion": 1,
        "clock": frame["clock"],
        "contextId": frame["contextId"],
        "epoch": 0,
        "encodedAt": 1000,
        "hostId": host["hostId"],
        "publisherId": host["owner"]["publisherId"],
        "serverGeneration": host["owner"]["serverGeneration"],
        "receipt": {
            "state": "ready",
            "preparedAt": 900,
            "associationStartedAt": 800,
            "ownerExpiresAt": 10100,
            "associationExpiresAt": 4000,
            "expiresAt": 4000,
            "error": None,
        },
        "rows": [
            {
                "sessionRef": {
                    key: session[key]
                    for key in ("hostId", "serverGeneration", "sessionId", "createdAt")
                },
                "attachedClients": 1,
                "association": {
                    "state": "open",
                    "resolvedAt": 100,
                    "reason": "retained_native_association",
                },
            }
        ],
    }
    return frame


class RetainedDisplayTests(unittest.TestCase):
    def setUp(self):
        self.frame = retained_frame()
        self.now = 1000
        self.scope = self.frame["clock"], self.frame["contextId"]
        # Semantic validation belongs to the separately tested producer contract;
        # these tests exercise consumer translation and revocation under that API.
        self.api = Mock()
        self.enterContext(
            patch("rofi_tmux_plus.prepared_model.observer_api", return_value=self.api)
        )
        self.enterContext(
            patch("rofi_tmux_plus.prepared_model.local_scope", return_value=self.scope)
        )
        self.enterContext(
            patch("rofi_tmux_plus.prepared_model.boottime_ms", side_effect=lambda: self.now)
        )
        self.enterContext(patch("rofi_tmux_plus.rofi.boottime_ms", side_effect=lambda: self.now))

    def payload(self):
        return project_frame(self.frame, now=self.now, scope=self.scope)

    def test_retained_match_is_qualified_and_attached_without_current_c3(self):
        payload = self.payload()
        self.api.bindings_contract.validate_fleet_bindings.assert_called_once()
        self.assertFalse(payload["viewerFactsCurrent"])
        host = payload["hosts"][0]
        self.assertTrue(host["viewerFactsCurrent"])
        self.assertEqual(host["sessions"][0]["localViewer"]["resolvedAt"], 100)
        for view in (rofi.VIEW_OPEN, rofi.VIEW_ATTACHED):
            rendered = rofi.render_snapshot(payload, now=0, navigation=rofi.NavigationState(view))
            self.assertIn('"sessionId":"$2"', rendered)
            self.assertIn("open here?", rendered)
        selected = rofi.selection_payload(host["sessions"][0], mesh_revision=None)
        self.assertNotIn("resolvedAt", selected)
        self.assertNotIn("localBindings", selected)
        self.assertNotIn("windowId", selected)

    def test_local_native_lease_and_remote_c3_expire_independently(self):
        view = self.frame["snapshot"]
        view["desktop"].update(state="ready", expiresAt=2000, error=None)
        remote = copy.deepcopy(view["hosts"][0])
        remote.update(hostId="remote", local=False)
        del remote["localBindings"]
        remote["sessions"][0].update(
            hostId="remote", localViewer={"state": "open", "confidence": "matched", "reason": None}
        )
        view["hosts"].append(remote)
        initial = self.payload()
        self.now = 2000
        expired = apply_expiry(initial)
        self.assertEqual([host["viewerFactsCurrent"] for host in expired["hosts"]], [True, False])
        self.assertEqual(expired["hosts"][0]["sessions"][0]["localViewer"]["state"], "open")
        self.now = 4000
        expired = apply_expiry(initial)
        self.assertFalse(expired["hosts"][0]["viewerFactsCurrent"])
        self.assertEqual(expired["hosts"][0]["sessions"][0]["localViewer"]["state"], "unknown")
        rendered = rofi.render_snapshot(
            initial, now=0, navigation=rofi.NavigationState(rofi.VIEW_ATTACHED)
        )
        self.assertIn('"sessionId":"$2"', rendered)

    def test_context_and_watch_loss_revoke_retained_display(self):
        payload = self.payload()
        changed = apply_expiry(payload, now=self.now, scope=(self.scope[0], "f" * 32))
        self.assertFalse(changed["hosts"][0]["viewerFactsCurrent"])
        with tempfile.TemporaryDirectory() as temporary:
            payload["prepared"]["watchRuntime"] = temporary
            write_notification(temporary, sequence=1, received=self.now, ready=False)
            lost = apply_expiry(payload)
        self.assertFalse(lost["hosts"][0]["viewerFactsCurrent"])
        self.assertEqual(lost["hosts"][0]["sessions"][0]["localViewer"]["state"], "unknown")

    def test_invalid_extension_stops_projection_without_native_fallback(self):
        self.api.bindings_contract.validate_fleet_bindings.side_effect = ValueError(
            "foreign source"
        )
        with (
            patch("subprocess.Popen", side_effect=AssertionError("native fallback")),
            self.assertRaisesRegex(ContractError, "Prepared local bindings are invalid"),
        ):
            self.payload()

    def test_pinned_contract_binds_real_projection_to_exact_owner_scope(self):
        actual = observer_api()
        with patch("rofi_tmux_plus.prepared_model.observer_api", return_value=actual):
            self.assertTrue(self.payload()["hosts"][0]["viewerFactsCurrent"])
            self.frame["snapshot"]["hosts"][0]["localBindings"]["publisherId"] = (
                "99999999-9999-4999-8999-999999999999"
            )
            with self.assertRaisesRegex(ContractError, "Prepared local bindings are invalid"):
                self.payload()

    def test_native_renewal_keeps_discovery_time_and_does_not_wake(self):
        first, expiry = notification_material(self.frame, self.now)
        changed = copy.deepcopy(self.frame)
        receipt = changed["snapshot"]["hosts"][0]["localBindings"]["receipt"]
        for key in ("preparedAt", "associationStartedAt", "associationExpiresAt", "expiresAt"):
            receipt[key] += 1000
        changed["snapshot"]["hosts"][0]["localBindings"]["encodedAt"] += 1000
        renewed, renewed_expiry = notification_material(changed, self.now)
        self.assertEqual(first, renewed)
        self.assertEqual(renewed_expiry, expiry + 1000)
        self.frame = changed
        self.now += 1000
        self.assertEqual(
            self.payload()["hosts"][0]["sessions"][0]["localViewer"]["resolvedAt"], 100
        )
        changed["snapshot"]["hosts"][0]["localBindings"]["rows"][0]["association"].update(
            state="unknown", resolvedAt=None, reason="process_unavailable"
        )
        self.assertNotEqual(first, notification_material(changed, self.now)[0])
        self.assertNotEqual(first, notification_material(self.frame, renewed_expiry)[0])
