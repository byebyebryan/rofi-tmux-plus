from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reference_frontend.inventory_service import InventoryService
from reference_frontend.picker_model import PickerModelService, RemoteRefresh, ViewerRefresh
from reference_frontend.remote_cache import RemoteCache
from reference_frontend.viewer_cache import ViewerObservationCache
from test_remote_cache import _Adapter, _LocalTmux, _row, _snapshot

from rofi_tmux_plus.config import Config


class RefreshRefinementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = time.time_ns() // 1_000_000
        self.cache = RemoteCache(Path(temporary.name) / "cache", now_millis=lambda: self.now)
        self.mesh = _snapshot(revision="sha256:" + "a" * 64, remotes=("beta", "gamma"))
        self.adapter = _Adapter(self.mesh)
        self.viewer = ViewerObservationCache(
            self.cache.directory, now_millis=lambda: self.now, context_id=lambda: "desktop"
        )

    def test_fast_peer_is_published_while_slow_peer_is_still_running(self) -> None:
        release = threading.Event()
        published = threading.Event()
        self.addCleanup(release.set)
        cache = self.cache
        mesh = self.mesh
        now = self.now

        class Peers:
            def inventory(self, host, *_args, **_kwargs):
                if host.host_id == "gamma":
                    self_wait = release.wait(3)
                    if not self_wait:
                        raise AssertionError("test did not release the slow peer")
                return _row(host.host_id, observed=now)

        def factory(config, **kwargs):
            original = kwargs["on_host"]

            def on_host(snapshot, host, row):
                original(snapshot, host, row)
                if host.host_id == "beta":
                    self.assertEqual([item["hostId"] for item in cache.load(mesh).hosts], ["beta"])
                    published.set()

            kwargs["on_host"] = on_host
            return InventoryService(config, remote_inventory=Peers(), **kwargs)

        refresh = RemoteRefresh(
            Config(), self.cache, mesh_adapter=self.adapter, inventory_factory=factory
        )
        thread = threading.Thread(target=refresh.run, args=(self.mesh.revision,))
        thread.start()
        try:
            self.assertTrue(published.wait(2))
            self.assertTrue(thread.is_alive())
            self.assertEqual(refresh.status(self.mesh.revision)["state"], "running")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(
            [row["hostId"] for row in self.cache.load(self.mesh).hosts], ["beta", "gamma"]
        )
        self.assertEqual(refresh.status(self.mesh.revision)["state"], "complete")

    def test_late_result_cannot_overwrite_newer_reconciliation_or_freshness(self) -> None:
        initial = [_row(host, observed=self.now) for host in ("beta", "gamma")]
        self.cache.merge(self.mesh, initial)
        older = self.cache.reserve(self.mesh, ["beta"])["beta"]
        newer = self.cache.reserve(self.mesh, ["beta"])["beta"]
        changed = _row("beta", observed=self.now + 100)
        changed["sessions"][0]["name"] = "renamed"
        self.cache.merge_host(self.mesh, "beta", changed, operation_token=newer)
        self.cache.merge_host(self.mesh, "beta", initial[0], operation_token=older)
        rows = self.cache.load(self.mesh).hosts
        self.assertEqual(rows[0]["sessions"][0]["name"], "renamed")
        self.assertEqual(rows[0]["ownerObservedAt"], self.now + 100)
        self.assertEqual(rows[1]["ownerObservedAt"], self.now)

    def test_viewer_job_reads_retained_owners_without_remote_inventory(self) -> None:
        self.cache.merge(self.mesh, [_row(host, observed=self.now) for host in ("beta", "gamma")])
        before = self.cache._state_path.read_bytes()
        scans = []

        class ObservationOnly:
            def inventory(self, **_kwargs):
                raise AssertionError("viewer renewal attempted SSH inventory")

            def _enrich_local_viewers(self, response, endpoint, _executable, *, deadline):
                scans.append([host["hostId"] for host in response["hosts"]])
                self_test.assertLessEqual(deadline - time.monotonic(), 3)
                for host in response["hosts"]:
                    for session in host["sessions"]:
                        session["localViewer"] = {"state": "none"}
                response["viewerEndpoint"] = {"hostId": endpoint, "observedAt": self_test.now}

        self_test = self
        refresh = ViewerRefresh(
            Config(),
            self.cache,
            viewer_cache=self.viewer,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: ObservationOnly(),
        )
        with patch("reference_frontend.picker_model.TmuxClient", return_value=_LocalTmux()):
            self.assertTrue(refresh.run(self.mesh.revision))
        self.assertEqual(scans, [["alpha", "beta", "gamma"]])
        self.assertEqual(refresh.status(self.mesh.revision)["state"], "complete")
        self.assertEqual(before, self.cache._state_path.read_bytes())
        self.assertIsNone(RemoteRefresh(Config(), self.cache).status(self.mesh.revision))

    def test_changed_owner_facts_invalidate_fresh_positive_viewer(self) -> None:
        owner = _row("beta", observed=self.now)
        owner["sessions"][0]["localViewer"] = {"state": "open", "confidence": "matched"}
        response = {
            "schemaVersion": 1,
            "meshRevision": self.mesh.revision,
            "viewerEndpoint": {"hostId": "alpha", "observedAt": self.now},
            "hosts": [owner],
        }
        self.assertTrue(
            self.viewer.merge_response(
                response,
                mesh_revision=self.mesh.revision,
                expected_context="desktop",
                endpoint_host_id="alpha",
                expected_host_ids=["beta"],
            )
        )
        for change in ("attachedClients", "pending", "name", "route"):
            value = copy.deepcopy(response)
            if change == "route":
                value["hosts"][0][change] = "other.route"
            else:
                value["hosts"][0]["sessions"][0][change] = {
                    "attachedClients": 0,
                    "pending": True,
                    "name": "renamed",
                }[change]
            self.assertTrue(
                self.viewer.decorate(
                    value, mesh_revision=self.mesh.revision, endpoint_host_id="alpha"
                )
            )
            self.assertEqual(value["hosts"][0]["sessions"][0]["localViewer"]["state"], "unknown")

    def test_request_lock_deduplicates_concurrent_spawn_gap(self) -> None:
        commands = []
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def spawn(argv):
            commands.append(argv)
            entered.set()
            release.wait(3)

        first = RemoteRefresh(Config(), self.cache, process_starter=spawn)
        second = RemoteRefresh(Config(), self.cache, process_starter=spawn)
        thread = threading.Thread(target=first.request, args=(self.mesh,))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(second.request(self.mesh))
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(commands), 1)

    def test_new_owner_inputs_renew_immediately_but_failed_scans_keep_cooldown(self) -> None:
        owner = _row("beta", observed=self.now)
        owner["sessions"][0]["localViewer"] = {"state": "none"}
        response = {
            "schemaVersion": 1,
            "meshRevision": self.mesh.revision,
            "viewerEndpoint": {"hostId": "alpha", "observedAt": self.now},
            "hosts": [owner],
        }
        self.viewer.merge_response(
            response,
            mesh_revision=self.mesh.revision,
            expected_context="desktop",
            endpoint_host_id="alpha",
            expected_host_ids=["beta"],
        )
        owner["sessions"][0].pop("localViewer")
        owner["sessions"][0]["name"] = "new name"
        self.cache.merge(self.mesh, [owner, _row("gamma", observed=self.now)])
        commands = []
        refresher = ViewerRefresh(Config(), self.cache, process_starter=commands.append)
        refresher._cache.write_marker("complete", self.mesh.revision)
        model = PickerModelService(
            Config(),
            cache=self.cache,
            viewer_cache=self.viewer,
            mesh_adapter=self.adapter,
            local_tmux=_LocalTmux(),
            viewer_refresher=refresher,
            now=lambda: self.now,
        )
        self.assertTrue(model.load().payload["viewerRefreshRequested"])
        self.assertEqual(len(commands), 1)
        refresher._cache.write_marker("failed", self.mesh.revision)
        self.assertFalse(model.load().payload["viewerRefreshRequested"])
        self.assertEqual(len(commands), 1)

    def test_clock_rollback_does_not_leave_running_marker_or_positive_cache_forever(self) -> None:
        self.cache.write_marker("running", self.mesh.revision)
        owner = _row("beta", observed=self.now)
        owner["sessions"][0]["localViewer"] = {"state": "open", "confidence": "matched"}
        response = {
            "schemaVersion": 1,
            "meshRevision": self.mesh.revision,
            "viewerEndpoint": {"hostId": "alpha", "observedAt": self.now},
            "hosts": [owner],
        }
        options = {
            "mesh_revision": self.mesh.revision,
            "expected_context": "desktop",
            "endpoint_host_id": "alpha",
            "expected_host_ids": ["beta"],
        }
        self.assertTrue(self.viewer.merge_response(response, **options))
        self.now -= 60_000
        self.assertIsNone(
            self.cache.marker(mesh_revision=self.mesh.revision, stall_after_seconds=20)
        )
        response["viewerEndpoint"]["observedAt"] = self.now
        owner["sessions"][0]["localViewer"] = {"state": "none"}
        self.assertTrue(self.viewer.merge_response(response, **options))
