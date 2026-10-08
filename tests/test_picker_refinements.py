from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_rofi import (
    FakeLifecycle,
    FakeModel,
    host,
    payload,
    rendered_records,
    row_options,
    session,
)

from rofi_tmux_plus import cli, launcher, rofi
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.presentation_cache import PresentationSnapshotCache
from rofi_tmux_plus.view_preferences import ViewPreferenceStore


class PickerRefinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = {
            "XDG_STATE_HOME": str(Path(self.temporary.name) / "state"),
            "XDG_CACHE_HOME": str(Path(self.temporary.name) / "cache"),
        }
        self.cache = PresentationSnapshotCache(environ=self.environment)
        self.store = ViewPreferenceStore("alpha", environ=self.environment)
        first = session("alpha", "$0", "first", activity=200, attached=1)
        second = session("alpha", "$1", "second", activity=100, attached=0)
        first["localViewer"] = {"state": "open", "confidence": "confirmed"}
        second["localViewer"] = {"state": "none"}
        owner = host("alpha", "Alpha", local=True, sessions=[first, second])
        owner["observedAt"] = 100_000
        self.value = payload(hosts=[owner])
        self.value.update(
            viewerObservedAt=100_000,
            viewerEndpoint={"hostId": "alpha", "observedAt": 100_000},
            ownerFreshnessSeconds=30,
        )

    def invoke(self, environment, *, lifecycle=None):
        output = io.StringIO()
        with redirect_stdout(output), patch("rofi_tmux_plus.rofi.time.time", return_value=100):
            rofi.run_rofi(
                dict(self.environment, **environment),
                model_service=FakeModel(self.value),
                lifecycle_service=lifecycle or FakeLifecycle(),
                presentation_cache=self.cache,
            )
        return output.getvalue()

    def test_bookmark_selects_same_reference_after_rename_and_never_late_restores(self) -> None:
        target = self.value["hosts"][0]["sessions"][1]
        self.assertTrue(self.store.update("local", None, opened=target))
        target["name"] = "renamed"
        frame = self.invoke({"ROFI_RETV": "0"})
        self.assertEqual(launcher.selected_row(frame), 1)
        self.assertIn("Tmux › Local", frame)
        self.assertIn("[Open]", frame)
        target["serverGeneration"] = "tmux-v1:replacement"
        frame = self.invoke({"ROFI_RETV": "0"})
        self.assertEqual(launcher.selected_row(frame), 0)
        headers, _rows = rendered_records(frame)
        data = next(item.split("\x1f", 1)[1] for item in headers if item.startswith("\0data"))
        self.assertIsNone(rofi._parse_continuation_state(data).highlighted)
        self.assertEqual(
            self.store.load().last_used["serverGeneration"], "tmux-v1:alpha:generation"
        )

    def test_only_successful_open_updates_bookmark_and_write_failure_keeps_success(self) -> None:
        target = self.value["hosts"][0]["sessions"][1]
        state = rofi.ContinuationState(
            navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "alpha"),
            snapshot_key=self.cache.store(self.value),
        )
        env = {
            "ROFI_RETV": "1",
            "ROFI_DATA": rofi._state_data(state),
            "ROFI_INFO": rofi.selection_payload(
                dict(target, _host=self.value["hosts"][0]),
                mesh_revision=self.value["meshRevision"],
            ),
        }
        self.invoke(env, lifecycle=FakeLifecycle(ContractError("stale_session", "gone")))
        self.assertIsNone(self.store.load().last_used)
        self.assertEqual(self.invoke(env), "")
        self.assertEqual(self.store.load().last_used["sessionId"], "$1")
        with patch.object(ViewPreferenceStore, "update", side_effect=OSError("disk full")):
            self.assertEqual(self.invoke(env), "")

    def test_view_change_is_persisted_without_model_or_scan_and_preserves_last_used(self) -> None:
        target = self.value["hosts"][0]["sessions"][1]
        self.store.update("local", None, opened=target)
        state = rofi.ContinuationState(
            navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "alpha"),
            snapshot_key=self.cache.store(self.value),
        )
        with patch(
            "rofi_tmux_plus.rofi.PickerModelService", side_effect=AssertionError("discovery")
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                rofi.run_rofi(
                    dict(self.environment, ROFI_RETV="11", ROFI_DATA=rofi._state_data(state)),
                    presentation_cache=self.cache,
                )
        self.assertEqual(self.store.load().view, "open")
        self.assertEqual(self.store.load().last_used["sessionId"], "$1")
        self.assertIn("keep-filter", output.getvalue())
        self.assertIn("Tmux › Open", self.invoke({"ROFI_RETV": "0"}))

    def test_open_and_attached_are_independent_and_stale_owner_cannot_be_refreshed_by_scan(
        self,
    ) -> None:
        for view in (rofi.VIEW_OPEN, rofi.VIEW_ATTACHED):
            rows = rendered_records(
                rofi.render_snapshot(self.value, navigation=rofi.NavigationState(view), now=100)
            )[1]
            self.assertEqual([row.split("\0", 1)[0] for row in rows], ["first"])
        self.value["hosts"][0]["sessions"][0]["localViewer"] = {
            "state": "unknown",
            "reason": "compositor_unavailable",
        }
        rows = rendered_records(
            rofi.render_snapshot(
                self.value, navigation=rofi.NavigationState(rofi.VIEW_ATTACHED), now=100
            )
        )[1]
        self.assertEqual(rows[0].split("\0", 1)[0], "first")
        self.value["hosts"][0]["sessions"][0]["localViewer"] = {
            "state": "open",
            "confidence": "matched",
        }
        frame = rofi.render_snapshot(
            self.value, navigation=rofi.NavigationState(rofi.VIEW_OPEN), now=100
        )
        self.assertIn("open here?", frame)
        self.value["viewerObservedAt"] = 131_000
        for view in (rofi.VIEW_OPEN, rofi.VIEW_ATTACHED):
            rows = rendered_records(
                rofi.render_snapshot(self.value, navigation=rofi.NavigationState(view), now=131)
            )[1]
            self.assertEqual(row_options(rows[0])["nonselectable"], "true")
            self.assertIn("unknown", row_options(rows[0])["display"].lower())

    def test_removed_filtered_selection_resets_to_eligible_row_and_empty_views_remain(self) -> None:
        state = rofi.ContinuationState(
            navigation=rofi.NavigationState(rofi.VIEW_ATTACHED),
            highlighted=rofi._highlighted_selection(
                dict(self.value["hosts"][0]["sessions"][1], meshRevision=self.value["meshRevision"])
            ),
        )
        frame = rofi.render_snapshot(self.value, state=state, preserve=True, now=100)
        self.assertIn("\0new-selection\x1f0", frame)
        self.value["hosts"][0]["sessions"][0]["attachedClients"] = 0
        frame = rofi.render_snapshot(self.value, state=state, preserve=True, now=100)
        self.assertIn("Tmux › Attached", frame)
        self.assertIn("No attached sessions", frame)

    def test_preferences_reject_unsafe_files_and_scope_endpoint(self) -> None:
        self.store.update("open", None, opened=self.value["hosts"][0]["sessions"][0])
        self.assertIsNone(ViewPreferenceStore("beta", environ=self.environment).load().last_used)
        original = self.store.path.read_bytes()
        self.store.path.unlink()
        target = Path(self.temporary.name) / "other"
        target.write_bytes(original)
        self.store.path.symlink_to(target)
        self.assertIsNone(self.store.load().last_used)
        self.store.path.unlink()
        os.mkfifo(self.store.path)
        self.assertIsNone(self.store.load().last_used)
        self.store.path.unlink()
        self.store.directory.chmod(0o755)
        self.assertFalse(self.store.update("open", None))
        self.assertEqual(target.read_bytes(), original)

    def test_expired_owner_facts_keep_unavailable_warning_precedence(self) -> None:
        owner = dict(
            self.value["hosts"][0], status="unreachable", stale=True, ownerFactsExpired=True
        )
        self.assertEqual(
            rofi._session_status(self.value["hosts"][0]["sessions"][0], owner, viewer_fresh=True),
            "unavailable",
        )

    def test_idle_tick_uses_cached_frame_until_renewal_and_initial_frame_arms_preservation(
        self,
    ) -> None:
        frame = self.invoke({"ROFI_RETV": "0"})
        self.assertIn("\0keep-selection\x1ftrue", frame)
        self.assertIn("\0keep-filter\x1ftrue", frame)
        state = rofi.ContinuationState(
            navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "alpha"),
            snapshot_key=self.cache.store(self.value),
            viewer_deadline=107,
        )
        model = FakeModel(self.value)
        frame = rofi._auto_refresh_callback(model, state, now=101, presentation_cache=self.cache)
        self.assertEqual(model.calls, [])
        self.assertIn("first", frame)
        rofi._auto_refresh_callback(model, state, now=107, presentation_cache=self.cache)
        self.assertEqual(model.calls, [False])

    def test_launcher_prepares_once_serves_exact_frame_and_cleans_it(self) -> None:
        frame = "\0new-selection\x1f3\nprepared\n"
        seen = []

        def run(arguments, *, env, check):
            self.assertIn("3", arguments)
            path = Path(env[launcher.INITIAL_FRAME_ENV])
            self.assertEqual(launcher.initial_frame(dict(env, ROFI_RETV="0")), frame)
            self.assertIsNone(launcher.initial_frame(dict(env, ROFI_RETV="28")))
            seen.append(path)
            return type("Result", (), {"returncode": 0})()

        with patch(
            "rofi_tmux_plus.rofi.run_rofi", side_effect=lambda _env: print(frame, end="")
        ) as prepare:
            with patch("rofi_tmux_plus.launcher.subprocess.run", side_effect=run):
                self.assertEqual(launcher.main(["-show", "tmux-plus"]), 0)
            prepare.assert_called_once()
        self.assertFalse(seen[0].exists())

    def test_cli_first_callback_serves_frame_without_discovery(self) -> None:
        path = Path(self.temporary.name) / "initial"
        path.write_text("prepared\n")
        path.chmod(0o600)
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"ROFI_RETV": "0", launcher.INITIAL_FRAME_ENV: str(path)}),
            patch("rofi_tmux_plus.rofi.run_rofi", side_effect=AssertionError("second preparation")),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(output.getvalue(), "prepared\n")
        self.assertIsNone(
            launcher.initial_frame({"ROFI_RETV": "0", launcher.INITIAL_FRAME_ENV: str(path)})
        )
