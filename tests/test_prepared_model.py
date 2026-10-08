"""Prepared expiry, exact ticket ownership and read-only picker adoption."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from rofi_tmux_plus import rofi
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.picker_runtime import read_private, write_private
from rofi_tmux_plus.prepared_model import (
    RUNTIME_ENV,
    PreparedModelService,
    apply_expiry,
    project_frame,
)
from rofi_tmux_plus.presentation_cache import PresentationSnapshotCache


class PreparedModelTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="rofi-tmux-plus-prepared-test-")
        self.addCleanup(self.root.cleanup)
        self.environment = {RUNTIME_ENV: self.root.name}
        self.frame = json.loads(
            (Path(__file__).parent / "fixtures/prepared-fleet-frame.json").read_text()
        )
        self.context = self.frame["contextId"]
        self.clock = self.frame["clock"]
        self.now = 1000
        self.api = Mock()
        self.api.desktop_context_id.return_value = self.context
        self.api.read_cached.side_effect = lambda *_a, **_kw: copy.deepcopy(self.frame)
        self.cache = PresentationSnapshotCache(directory=Path(self.root.name) / "cache")
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch("rofi_tmux_plus.prepared_model.local_scope", self.scope))
        self.stack.enter_context(
            patch("rofi_tmux_plus.prepared_model.boottime_ms", side_effect=lambda: self.now)
        )
        self.stack.enter_context(
            patch("rofi_tmux_plus.rofi.boottime_ms", side_effect=lambda: self.now)
        )

    def scope(self, _environ=None):
        return self.clock, self.context

    def model(self, *, environment=None):
        return PreparedModelService(
            environ=self.environment if environment is None else environment,
            api=self.api,
            now=lambda: self.now,
            scope=self.scope,
        )

    def fresh_desktop(self):
        self.frame["snapshot"]["desktop"].update(state="ready", expiresAt=5000, error=None)
        self.frame["snapshot"]["hosts"][0]["sessions"][0].update(
            attachedClients=2,
            localViewer={"state": "open", "confidence": "confirmed", "reason": None},
        )

    def ticket(self, state="running", *, publisher=None, ticket_id=None):
        return {
            "id": ticket_id or "44444444-4444-4444-8444-444444444444",
            "publisherId": publisher or self.frame["readerId"],
            "state": state,
            "requestedAt": 1000,
            "deadlineAt": 16000,
            "sources": [
                {
                    "hostId": "fixture-local",
                    "source": "owner",
                    "state": state,
                    "attempt": 2,
                    "error": None,
                }
            ],
        }

    def admission(self, state="running"):
        result = copy.deepcopy(self.frame)
        result["ticket"] = self.ticket(state)
        self.api.read_cached.side_effect = [copy.deepcopy(self.frame), result]
        return self.model().refresh_now()

    def test_startup_is_one_cached_read_even_with_old_refresh_flag(self):
        with patch("subprocess.Popen", side_effect=AssertionError("native collection")):
            model = self.model().load(start_refresh=True)
        self.assertFalse(model.refresh_needed)
        self.api.read_cached.assert_called_once_with(self.context)
        self.assertNotIn("remoteRefresh", model.payload)
        self.assertEqual(list(Path(self.root.name).iterdir()), [])

    def test_absent_or_warming_reader_cannot_start_a_service_or_fallback(self):
        for error in (FileNotFoundError("absent"), TimeoutError("warming")):
            with self.subTest(error=type(error).__name__):
                self.api.read_cached.reset_mock()
                self.api.read_cached.side_effect = error
                with (
                    patch("subprocess.Popen", side_effect=AssertionError),
                    self.assertRaisesRegex(ContractError, "Prepared Tmux Observer unavailable"),
                ):
                    self.model().load(start_refresh=True)
                self.api.read_cached.assert_called_once_with(self.context)
                self.assertEqual(list(Path(self.root.name).iterdir()), [])

    def test_owner_and_desktop_expire_independently_on_suspend_aware_clock(self):
        self.fresh_desktop()
        initial = self.model().load().payload
        self.assertEqual(initial["hosts"][0]["status"], "ok")
        self.assertTrue(initial["viewerFactsCurrent"])
        self.now = 5000
        expired = apply_expiry(initial)
        self.assertEqual(expired["hosts"][0]["status"], "ok")
        self.assertFalse(expired["viewerFactsCurrent"])
        self.now = 10100
        expired = apply_expiry(initial)
        self.assertEqual(expired["hosts"][0]["status"], "stale")
        self.assertTrue(expired["hosts"][0]["ownerFactsExpired"])
        self.assertEqual(initial["hosts"][0]["status"], "ok")

    def test_remote_clock_is_not_used_as_local_authority(self):
        remote = copy.deepcopy(self.frame["snapshot"]["hosts"][0])
        remote.update(hostId="remote", local=False)
        remote["owner"]["clock"] = {"bootId": "foreign", "timeNamespace": "time:999"}
        remote["owner"]["receipt"]["expiresAt"] = 2**63 - 1
        remote["owner"]["localExpiry"] = 2000
        remote["sessions"] = []
        self.frame["snapshot"]["hosts"].append(remote)
        initial = self.model().load().payload
        self.now = 2000
        expired = apply_expiry(initial)
        self.assertEqual([host["status"] for host in expired["hosts"]], ["ok", "stale"])

    def test_boot_namespace_context_change_and_clock_regression_revoke_positives(self):
        self.fresh_desktop()
        initial = self.model().load().payload
        for clock, context, now in (
            ({**self.clock, "bootId": "different"}, self.context, 1000),
            ({**self.clock, "timeNamespace": "time:999"}, self.context, 1000),
            (self.clock, "f" * 32, 1000),
            (self.clock, self.context, 999),
        ):
            with self.subTest(clock=clock, context=context, now=now):
                value = apply_expiry(initial, now=now, scope=(clock, context))
                self.assertEqual(value["hosts"][0]["status"], "stale")
                self.assertFalse(value["viewerFactsCurrent"])

    def test_rendering_old_presentation_rechecks_lease_and_preserves_reference(self):
        self.fresh_desktop()
        initial = self.model().load().payload
        reference = initial["hosts"][0]["sessions"][0]
        for wall_clock in (0, 2**31):
            with self.subTest(wall_clock=wall_clock):
                self.now = 1000
                for view in (rofi.VIEW_OPEN, rofi.VIEW_ATTACHED):
                    frame = rofi.render_snapshot(
                        initial, now=wall_clock, navigation=rofi.NavigationState(view)
                    )
                    self.assertIn('"sessionId":"$2"', frame)
                self.now = 31000
                for view in (rofi.VIEW_OPEN, rofi.VIEW_ATTACHED):
                    frame = rofi.render_snapshot(
                        initial, now=wall_clock, navigation=rofi.NavigationState(view)
                    )
                    self.assertNotIn('"sessionId":"$2"', frame)
                frame = rofi.render_snapshot(
                    initial,
                    now=wall_clock,
                    navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "fixture-local"),
                )
                self.assertIn("unavailable", frame)
                self.assertIn('"sessionId":"$2"', frame)
                self.assertIn(str(reference["createdAt"]), frame)
                self.assertIn(reference["serverGeneration"], frame)

    def test_explicit_ticket_survives_new_callback_and_equal_rows_terminal_lookup(self):
        admitted = self.admission()
        state, message = rofi._refresh_observation(
            admitted.payload, rofi.ContinuationState(), now=10
        )
        self.assertEqual(message, "Refreshing")
        self.assertIsNotNone(state.refresh_deadline)
        terminal = copy.deepcopy(self.frame)
        terminal["ticket"] = self.ticket("complete")
        self.api.read_cached.reset_mock()
        self.api.read_cached.side_effect = [terminal]
        completed = self.model().load(start_refresh=True)
        self.api.read_cached.assert_called_once_with(
            self.context,
            operation="refresh_status",
            expected_host="fixture-local",
            publisher_id=self.frame["readerId"],
            ticket_id=self.ticket()["id"],
        )
        self.assertEqual(
            admitted.payload["prepared"]["viewRevision"],
            completed.payload["prepared"]["viewRevision"],
        )
        self.assertEqual(admitted.payload["hosts"], completed.payload["hosts"])
        state, message = rofi._refresh_observation(completed.payload, state, now=11)
        self.assertIsNone(state.refresh_deadline)
        self.assertEqual(message, "")
        self.assertEqual(completed.payload["remoteRefresh"]["state"], "complete")

    def test_unrelated_ticket_cannot_complete_or_clear_owned_request(self):
        self.admission()
        foreign = copy.deepcopy(self.frame)
        foreign["ticket"] = self.ticket(
            "complete", ticket_id="55555555-5555-4555-8555-555555555555"
        )
        self.api.read_cached.side_effect = [foreign, copy.deepcopy(self.frame)]
        value = self.model().load().payload
        self.assertEqual(value["remoteRefresh"]["ticketId"], self.ticket()["id"])
        self.assertEqual(value["remoteRefresh"]["state"], "failed")
        self.assertIn("matching ticket", value["remoteRefresh"]["message"])

    def test_deadline_is_terminal_without_readmitting_or_retrying_sources(self):
        self.admission()
        self.now = 16000
        self.api.read_cached.reset_mock()
        self.api.read_cached.side_effect = lambda *_a, **_kw: copy.deepcopy(self.frame)
        value = self.model().load().payload
        self.api.read_cached.assert_called_once_with(self.context)
        self.assertEqual(value["remoteRefresh"]["state"], "stalled")
        self.api.read_cached.reset_mock()
        self.model().load(start_refresh=True)
        self.api.read_cached.assert_called_once_with(self.context)

    def test_reader_loss_ends_owned_notice_and_adopts_new_scoped_snapshot(self):
        self.admission()
        replacement = copy.deepcopy(self.frame)
        replacement["readerId"] = "55555555-5555-4555-8555-555555555555"
        replacement["snapshot"]["readerId"] = replacement["readerId"]
        self.api.read_cached.reset_mock()
        self.api.read_cached.side_effect = [OSError("reader incarnation changed"), replacement]
        value = self.model().load().payload
        self.assertEqual(value["prepared"]["readerId"], replacement["readerId"])
        self.assertEqual(value["remoteRefresh"]["state"], "failed")
        self.assertEqual(self.api.read_cached.call_count, 2)
        self.assertEqual(self.api.read_cached.call_args_list[-1].kwargs, {})

    def test_terminal_failure_notice_has_fixed_lifetime(self):
        value = self.admission("failed").payload
        value["remoteRefresh"]["message"] = "native attempt failed"
        state, message = rofi._refresh_observation(value, rofi.ContinuationState(), now=10)
        self.assertEqual(message, "native attempt failed")
        deadline = state.error_deadline
        state, message = rofi._refresh_observation(value, state, now=11)
        self.assertEqual(state.error_deadline, deadline)
        self.assertEqual(message, "native attempt failed")
        state, message = rofi._refresh_observation(value, state, now=deadline)
        self.assertIsNone(state.error_deadline)
        self.assertEqual(message, "")

    def test_scoped_reconciliation_requests_only_affected_owner_and_checks_revision(self):
        model = self.model()
        reply = copy.deepcopy(self.frame)
        reply["ticket"] = self.ticket()
        self.api.read_cached.side_effect = [copy.deepcopy(self.frame), reply]
        model.refresh_host("fixture-local", None)
        self.assertEqual(
            self.api.read_cached.call_args.kwargs["sources"],
            [{"hostId": "fixture-local", "source": "owner"}],
        )
        self.api.read_cached.reset_mock()
        self.api.read_cached.side_effect = [copy.deepcopy(self.frame)]
        with self.assertRaises(ContractError) as caught:
            model.refresh_host("fixture-local", "sha256:" + "b" * 64)
        self.assertEqual(caught.exception.code, "stale_mesh")
        self.api.read_cached.assert_called_once_with(self.context)

    def test_refresh_without_owned_or_with_unsafe_runtime_refuses_before_admission(self):
        with self.assertRaisesRegex(ContractError, "launcher-owned"):
            self.model(environment={}).refresh_now()
        self.api.read_cached.assert_not_called()
        Path(self.root.name).chmod(0o755)
        with self.assertRaises(ValueError):
            self.model().refresh_now()
        self.api.read_cached.assert_not_called()
        Path(self.root.name).chmod(0o700)

    def test_cached_navigation_expires_without_model_or_lifecycle_calls(self):
        self.fresh_desktop()
        value = self.model().load().payload
        state = rofi.ContinuationState(snapshot_key=self.cache.store(value))
        self.now = 31000
        output = io.StringIO()
        with (
            patch("rofi_tmux_plus.rofi.PreparedModelService", side_effect=AssertionError("model")),
            patch("rofi_tmux_plus.rofi.LifecycleService", side_effect=AssertionError("action")),
            redirect_stdout(output),
        ):
            rofi.run_rofi(
                {"ROFI_RETV": "11", "ROFI_DATA": rofi._state_data(state)},
                presentation_cache=self.cache,
            )
        self.assertNotIn('"sessionId":"$2"', output.getvalue())
        self.assertIn("unknown", output.getvalue())

    def test_adoption_preserves_pending_target_without_collecting_or_executing_action(self):
        value = self.model().load().payload
        session = rofi._session_rows(value)[0]
        selection = json.loads(rofi.selection_payload(session, mesh_revision=None))
        pending = rofi._new_action(
            "confirm-kill",
            rofi.NavigationState(rofi.VIEW_LOCAL, "fixture-local"),
            selection=selection,
        )
        state = rofi.ContinuationState(
            pending_action=pending,
            navigation=pending.origin,
            snapshot_key=self.cache.store(value),
            viewer_deadline=2**40,
        )
        self.frame["snapshot"]["hosts"][0]["sessions"][0].update(
            name="replacement", createdAt=session["createdAt"] + 1
        )
        self.api.read_cached.reset_mock()
        lifecycle = Mock()
        output = io.StringIO()
        with redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": "28", "ROFI_DATA": rofi._state_data(state)},
                model_service=self.model(),
                lifecycle_service=lifecycle,
                config=Config(),
                presentation_cache=self.cache,
            )
        rendered = output.getvalue()
        self.api.read_cached.assert_called_once_with(self.context)
        self.assertIn('"createdAt":' + str(session["createdAt"]), rendered)
        self.assertIn('"name":"fixture"', rendered)
        self.assertNotIn('"name":"replacement"', rendered)
        self.assertEqual(lifecycle.mock_calls, [])

    def test_mesh_error_is_unavailable_and_keeps_the_typed_cause(self):
        self.frame["snapshot"]["mesh"]["state"] = "error"
        self.frame["snapshot"]["error"] = {"code": "mesh_unavailable", "message": "catalog failed"}
        value = project_frame(self.frame, now=self.now, scope=self.scope())
        self.assertEqual(value["hosts"][0]["status"], "unavailable")
        self.assertEqual(value["hosts"][0]["error"]["message"], "catalog failed")


class PrivateRuntimeTests(unittest.TestCase):
    def test_symlinks_fifo_permissions_and_oversized_files_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"ordinary")
            target.chmod(0o600)
            path = root / "notify"
            path.symlink_to(target)
            with self.assertRaises(OSError):
                read_private(root, "notify", limit=16)
            path.unlink()
            os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError):
                read_private(root, "notify", limit=16)
            path.unlink()
            path.write_bytes(b"x" * 17)
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                read_private(root, "notify", limit=16)
            path.write_bytes(b"unsafe")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_private(root, "notify", limit=16)
            path.unlink()
            path.symlink_to(target)
            write_private(root, "notify", b"safe", limit=16)
            self.assertEqual(target.read_bytes(), b"ordinary")
            self.assertFalse(path.is_symlink())
            self.assertEqual(read_private(root, "notify", limit=16), b"safe")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["notify", "target"])
