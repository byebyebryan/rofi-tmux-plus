from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from rofi_tmux_plus import cli
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.mesh_adapter import (
    MeshHost,
    MeshPolicy,
    MeshRoute,
    MeshSnapshot,
    MeshStaleError,
)
from rofi_tmux_plus.picker_model import (
    _REFRESH_HARD_DEADLINE_SECONDS,
    _REFRESH_STALL_SECONDS,
    PickerModelService,
    RemoteRefresh,
    detached_refresh_command,
)
from rofi_tmux_plus.remote_cache import RemoteCache


def _snapshot(
    *, revision: str = "sha256:one", remotes: tuple[str, ...] = ("beta",)
) -> MeshSnapshot:
    hosts = [MeshHost("alpha", "Alpha", True, ("alpha-native",), ())]
    hosts.extend(
        MeshHost(
            host_id, host_id.title(), False, (), (MeshRoute(f"{host_id}.test", 0, None, None),)
        )
        for host_id in remotes
    )
    return MeshSnapshot(revision, "alpha", MeshPolicy("ssh", 2, 1, 300), tuple(hosts))


def _session(host_id: str, *, attached: int | None = 1) -> dict[str, object]:
    return {
        "hostId": host_id,
        "serverGeneration": "tmux-v1:10:20:/tmp/tmux",
        "sessionId": "$0",
        "createdAt": 11,
        "name": "session",
        "activityAt": 12,
        "lastAttachedAt": 12,
        "attachedClients": attached,
        "pending": False,
        "windowCount": 1,
        "sessionPath": "/tmp",
        "currentWindow": "shell",
        "currentPath": "/tmp",
    }


def _row(
    host_id: str,
    *,
    status: str = "ok",
    sessions: list[dict[str, object]] | None = None,
    observed: int = 100,
) -> dict[str, object]:
    result: dict[str, object] = {
        "hostId": host_id,
        "display": host_id.title(),
        "local": False,
        "status": status,
        "observedAt": observed,
        "nativeHostname": f"{host_id}-native",
        "serverGeneration": "tmux-v1:10:20:/tmp/tmux" if status == "ok" else None,
        "route": f"{host_id}.test" if status != "unreachable" else None,
        "sessions": [_session(host_id)] if sessions is None and status == "ok" else sessions or [],
    }
    if status != "ok":
        result["error"] = {"code": "operation_failed", "message": f"{status} now"}
    return result


class _Adapter:
    def __init__(self, snapshot: MeshSnapshot | None) -> None:
        self.snapshot = snapshot
        self.loads = 0

    def load(self) -> MeshSnapshot | None:
        self.loads += 1
        return self.snapshot


class _LocalTmux:
    def __init__(self) -> None:
        self.calls = 0

    def inventory(
        self, host_id: str, *, panes: bool, option_names: tuple[str, ...]
    ) -> tuple[None, list[object]]:
        self.calls += 1
        return None, []


class _Refresher:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(self, snapshot: MeshSnapshot) -> bool:
        self.requests.append(snapshot.revision)
        return True

    @staticmethod
    def status(mesh_revision: str) -> dict[str, object]:
        return {"state": "complete", "meshRevision": mesh_revision, "updatedAt": 1}


class RemoteCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1_000_000
        self.cache = RemoteCache(Path(self.temporary.name) / "cache", now_millis=lambda: self.now)
        self.snapshot = _snapshot()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_private_atomic_cache_and_corrupt_or_mismatched_state_is_ignored(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        state = self.cache.load(self.snapshot)
        assert state is not None
        self.assertEqual(state.hosts[0]["hostId"], "beta")
        self.assertEqual(stat.S_IMODE(self.cache.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cache._state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cache._lock_path.stat().st_mode), 0o600)
        self.cache._state_path.write_text("not-json", encoding="utf-8")
        self.assertIsNone(self.cache.load(self.snapshot))
        self.cache.merge(self.snapshot, [_row("beta")])
        self.assertIsNone(self.cache.load(_snapshot(revision="sha256:two")))
        self.assertIsNone(self.cache.load(_snapshot(remotes=())))

    def test_authoritative_empty_and_tmux_missing_clear_prior_sessions(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        self.now += 1
        self.cache.merge(self.snapshot, [_row("beta", sessions=[], observed=self.now)])
        state = self.cache.load(self.snapshot)
        assert state is not None
        self.assertEqual(state.hosts[0]["sessions"], [])
        self.assertEqual(state.hosts[0]["status"], "ok")
        self.now += 1
        self.cache.merge(self.snapshot, [_row("beta", status="tmux_missing", observed=self.now)])
        state = self.cache.load(self.snapshot)
        assert state is not None
        self.assertEqual(state.hosts[0]["status"], "tmux_missing")
        self.assertEqual(state.hosts[0]["sessions"], [])
        authoritative_seen = state.hosts[0]["lastSeenAt"]
        self.now += 1
        self.cache.merge(self.snapshot, [_row("beta", status="error", observed=self.now)])
        retained = self.cache.load(self.snapshot)
        assert retained is not None
        self.assertEqual(retained.hosts[0]["lastSeenAt"], authoritative_seen)
        self.assertTrue(retained.hosts[0]["unavailable"])

    def test_transport_and_domain_errors_retain_only_marked_historical_sessions(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        self.now += 1
        self.cache.merge(self.snapshot, [_row("beta", status="unreachable", observed=self.now)])
        unreachable = self.cache.load(self.snapshot)
        assert unreachable is not None
        row = unreachable.hosts[0]
        self.assertEqual(row["status"], "unreachable")
        self.assertIsNone(row["sessions"][0]["attachedClients"])
        self.assertTrue(row["stale"])
        self.assertTrue(row["unavailable"])
        self.now += 1
        self.cache.merge(self.snapshot, [_row("beta", status="error", observed=self.now)])
        error = self.cache.load(self.snapshot)
        assert error is not None
        row = error.hosts[0]
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["stale"])
        self.assertTrue(row["unavailable"])
        self.assertIsNone(row["sessions"][0]["attachedClients"])
        self.assertEqual(row["lastSeenAt"], 100)

    def test_marker_states_and_nonblocking_refresh_lock_are_observable(self) -> None:
        self.cache.write_marker("running", self.snapshot.revision)
        self.now += int(_REFRESH_HARD_DEADLINE_SECONDS * 1_000)
        self.assertEqual(
            self.cache.marker(
                mesh_revision=self.snapshot.revision,
                stall_after_seconds=_REFRESH_STALL_SECONDS,
            )["state"],
            "running",
        )
        self.now += (_REFRESH_STALL_SECONDS - int(_REFRESH_HARD_DEADLINE_SECONDS)) * 1_000 + 1
        self.assertEqual(
            self.cache.marker(
                mesh_revision=self.snapshot.revision,
                stall_after_seconds=_REFRESH_STALL_SECONDS,
            )["state"],
            "stalled",
        )
        adapter = _Adapter(self.snapshot)
        refresh = RemoteRefresh(Config(), self.cache, mesh_adapter=adapter)
        with self.cache.lock(refresh=True, blocking=True):
            self.assertFalse(refresh.run(self.snapshot.revision))

    def test_existing_locks_are_repaired_to_private_permissions(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        os.chmod(self.cache._lock_path, 0o644)
        with self.cache.lock():
            pass
        self.assertEqual(stat.S_IMODE(self.cache._lock_path.stat().st_mode), 0o600)
        with self.cache.lock(refresh=True):
            pass
        os.chmod(self.cache._refresh_lock_path, 0o644)
        with self.cache.lock(refresh=True):
            pass
        self.assertEqual(stat.S_IMODE(self.cache._refresh_lock_path.stat().st_mode), 0o600)

    def test_invalid_current_response_rows_preserve_prior_cache_atomically(self) -> None:
        snapshot = _snapshot(remotes=("beta", "gamma"))
        self.cache.merge(snapshot, [_row("beta"), _row("gamma")])
        before = self.cache._state_path.read_bytes()
        cases = (
            [_row("beta")],
            [_row("beta"), _row("beta")],
            [_row("beta"), _row("outside")],
            [_row("beta"), {}],
        )
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ContractError):
                self.cache.merge(snapshot, rows)
            self.assertEqual(self.cache._state_path.read_bytes(), before)

    def test_affected_host_merge_preserves_peer_and_does_not_freshen_peer_ttl(self) -> None:
        snapshot = _snapshot(remotes=("beta", "gamma"))
        self.cache.merge(snapshot, [_row("beta"), _row("gamma")])
        before = self.cache.load(snapshot)
        assert before is not None
        self.now += 500
        merged = self.cache.merge_host(snapshot, "beta", _row("beta", sessions=[]))
        self.assertEqual(before.written_at, merged.written_at)
        self.assertEqual(["beta", "gamma"], [row["hostId"] for row in merged.hosts])
        self.assertEqual([], merged.hosts[0]["sessions"])
        self.assertEqual("$0", merged.hosts[1]["sessions"][0]["sessionId"])
        with self.assertRaises(ContractError):
            self.cache.merge_host(snapshot, "beta", _row("gamma"))


class PickerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1_000_000
        self.snapshot = _snapshot()
        self.cache = RemoteCache(Path(self.temporary.name) / "cache", now_millis=lambda: self.now)
        self.local = _LocalTmux()
        self.refresher = _Refresher()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cache_miss_renders_live_local_without_remote_and_requests_refresh(self) -> None:
        model = PickerModelService(
            Config(refresh_seconds=30),
            cache=self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=self.refresher,  # type: ignore[arg-type]
            now=lambda: self.now,
        ).load()
        self.assertEqual([row["hostId"] for row in model.payload["hosts"]], ["alpha"])
        self.assertEqual(
            model.payload["hostCatalog"],
            [
                {"hostId": "alpha", "display": "Alpha", "local": True},
                {"hostId": "beta", "display": "Beta", "local": False},
            ],
        )
        self.assertTrue(model.payload["remoteRefreshNeeded"])
        self.assertEqual(self.refresher.requests, ["sha256:one"])
        self.assertEqual(self.local.calls, 1)

    def test_ttl_uses_written_time_not_mesh_fingerprint_and_retains_only_configured_hosts(
        self,
    ) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        model = PickerModelService(
            Config(refresh_seconds=30),
            cache=self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=self.refresher,  # type: ignore[arg-type]
            now=lambda: self.now,
        ).load(start_refresh=False)
        self.assertFalse(model.refresh_needed)
        self.assertEqual([row["hostId"] for row in model.payload["hosts"]], ["alpha", "beta"])
        self.now += 30_000
        expired = PickerModelService(
            Config(refresh_seconds=30),
            cache=self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=self.refresher,  # type: ignore[arg-type]
            now=lambda: self.now,
        ).load(start_refresh=False)
        self.assertTrue(expired.refresh_needed)

    def test_local_only_model_never_surfaces_a_stale_remote_marker(self) -> None:
        self.cache.write_marker("failed", self.snapshot.revision, message="old mesh failure")
        model = PickerModelService(
            Config(),
            cache=self.cache,
            mesh_adapter=_Adapter(None),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=self.refresher,  # type: ignore[arg-type]
            now=lambda: self.now,
        ).load()
        self.assertEqual(
            model.payload["hostCatalog"],
            [
                {
                    "hostId": model.payload["hosts"][0]["hostId"],
                    "display": model.payload["hosts"][0]["display"],
                    "local": True,
                }
            ],
        )
        self.assertIsNone(model.payload["remoteRefresh"])

    def test_spawn_failure_keeps_live_local_model_and_records_current_failure(self) -> None:
        def fail_spawn(_argv: object) -> None:
            raise OSError("fixture spawn failure")

        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            process_starter=fail_spawn,  # type: ignore[arg-type]
        )
        model = PickerModelService(
            Config(),
            cache=self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=refresh,
            now=lambda: self.now,
        ).load()
        self.assertEqual([row["hostId"] for row in model.payload["hosts"]], ["alpha"])
        self.assertFalse(model.payload["remoteRefreshRequested"])
        marker = self.cache.marker(
            mesh_revision=self.snapshot.revision,
            stall_after_seconds=_REFRESH_STALL_SECONDS,
        )
        assert marker is not None
        self.assertEqual(marker["state"], "failed")
        self.assertIn("fixture spawn failure", marker["message"])

    def test_old_revision_marker_does_not_suppress_current_refresh_request(self) -> None:
        self.cache.write_marker("running", "sha256:old")
        commands: list[list[str]] = []
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=_Adapter(self.snapshot),
            process_starter=lambda argv: commands.append(list(argv)),
        )
        self.assertTrue(refresh.request(self.snapshot))
        self.assertEqual(len(commands), 1)
        self.assertIn(self.snapshot.revision, commands[0])
        self.assertIsNone(refresh.status(self.snapshot.revision))

    def test_detached_command_resolves_external_tree_and_installed_console_script(self) -> None:
        root = Path(self.temporary.name) / "external-tree"
        package_file = root / "rofi_tmux_plus" / "picker_model.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        launcher = root / "bin" / "rofi-tmux-plus"
        launcher.parent.mkdir()
        launcher.touch()
        symlink = Path(self.temporary.name) / "linked-picker-model.py"
        symlink.symlink_to(package_file)
        self.assertEqual(
            detached_refresh_command("sha256:one", package_file=symlink, which=lambda _: None),
            [
                sys.executable,
                str(launcher),
                "_refresh",
                "--mesh-revision",
                "sha256:one",
            ],
        )
        launcher.unlink()
        self.assertEqual(
            detached_refresh_command(
                "sha256:one", package_file=package_file, which=lambda _: "/usr/bin/rofi-tmux-plus"
            ),
            ["/usr/bin/rofi-tmux-plus", "_refresh", "--mesh-revision", "sha256:one"],
        )
        with self.assertRaises(OSError):
            detached_refresh_command("sha256:one", package_file=package_file, which=lambda _: None)

    def test_source_launcher_imports_from_an_unrelated_working_directory(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "bin" / "rofi-tmux-plus"
        completed = subprocess.run(
            [sys.executable, str(launcher), "--help"],
            cwd=self.temporary.name,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("rofi-tmux-plus", completed.stdout)

    def test_affected_remote_refresh_is_revision_pinned_and_merges_only_that_host(self) -> None:
        snapshot = _snapshot(remotes=("beta", "gamma"))
        self.cache.merge(snapshot, [_row("beta"), _row("gamma")])
        inventory = _Inventory(
            {
                "schemaVersion": 1,
                "generatedAt": self.now,
                "meshRevision": snapshot.revision,
                "hosts": [_row("beta", sessions=[])],
            }
        )
        model_service = PickerModelService(
            Config(),
            cache=self.cache,
            mesh_adapter=_Adapter(snapshot),
            local_tmux=self.local,  # type: ignore[arg-type]
            refresher=self.refresher,  # type: ignore[arg-type]
            inventory_factory=lambda *_args, **_kwargs: inventory,  # type: ignore[arg-type]
            now=lambda: self.now,
        )
        model = model_service.refresh_host("beta", snapshot.revision)
        self.assertEqual(
            [
                {
                    "requested_hosts": ["beta"],
                    "mesh_revision": "sha256:one",
                    "panes": False,
                    "option_names": (),
                }
            ],
            inventory.calls,
        )
        self.assertEqual(
            ["alpha", "beta", "gamma"], [row["hostId"] for row in model.payload["hosts"]]
        )
        state = self.cache.load(snapshot)
        assert state is not None
        self.assertEqual([], state.hosts[0]["sessions"])
        self.assertEqual("$0", state.hosts[1]["sessions"][0]["sessionId"])
        with self.assertRaises(ContractError) as context:
            model_service.refresh_host("beta", "sha256:old")
        self.assertEqual("stale_mesh", context.exception.code)


class _Inventory:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def inventory(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RemoteRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1_000_000
        self.cache = RemoteCache(Path(self.temporary.name) / "cache", now_millis=lambda: self.now)
        self.snapshot = _snapshot()
        self.adapter = _Adapter(self.snapshot)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_revision_pinned_success_merges_remote_only_and_leaves_no_post_return_work(
        self,
    ) -> None:
        response = {
            "schemaVersion": 1,
            "generatedAt": self.now,
            "meshRevision": self.snapshot.revision,
            "hosts": [_row("beta")],
        }
        inventory = _Inventory(response)
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: inventory,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(inventory.calls[0]["requested_hosts"], ["beta"])
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "complete")
        state = self.cache.load(self.snapshot)
        assert state is not None
        before = json.dumps([*state.hosts], sort_keys=True)
        self.assertEqual(
            json.dumps([*self.cache.load(self.snapshot).hosts], sort_keys=True), before
        )

    def test_stale_and_failure_never_mix_new_rows(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        stale = _Inventory(MeshStaleError())
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: stale,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "stale")
        self.assertEqual(self.cache.load(self.snapshot).hosts[0]["sessions"][0]["sessionId"], "$0")
        failed = _Inventory(ContractError("operation_failed", "fake failure"))
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: failed,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "failed")

    def test_pinned_revision_mismatch_discards_refresh_before_remote_inventory(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        inventory = _Inventory(
            {
                "schemaVersion": 1,
                "generatedAt": self.now,
                "meshRevision": "sha256:two",
                "hosts": [_row("beta", sessions=[])],
            }
        )
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=_Adapter(_snapshot(revision="sha256:two")),
            inventory_factory=lambda *_args, **_kwargs: inventory,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "stale")
        self.assertEqual(inventory.calls, [])
        self.assertEqual(self.cache.load(self.snapshot).hosts[0]["sessions"][0]["sessionId"], "$0")

    def test_malformed_current_remote_set_fails_without_replacing_cache(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        before = self.cache._state_path.read_bytes()
        inventory = _Inventory(
            {
                "schemaVersion": 1,
                "generatedAt": self.now,
                "meshRevision": self.snapshot.revision,
                "hosts": [],
            }
        )
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: inventory,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "failed")
        self.assertEqual(self.cache._state_path.read_bytes(), before)

    def test_invalid_response_schema_is_observable_and_preserves_cache(self) -> None:
        self.cache.merge(self.snapshot, [_row("beta")])
        before = self.cache._state_path.read_bytes()
        inventory = _Inventory(
            {
                "schemaVersion": 2,
                "generatedAt": self.now,
                "meshRevision": self.snapshot.revision,
                "hosts": [_row("beta", sessions=[])],
            }
        )
        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=lambda *_args, **_kwargs: inventory,  # type: ignore[arg-type]
        )
        self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(refresh.status(self.snapshot.revision)["state"], "failed")
        self.assertEqual(self.cache._state_path.read_bytes(), before)

    def test_remote_inventory_receives_only_the_remaining_owner_deadline(self) -> None:
        response = {
            "schemaVersion": 1,
            "generatedAt": self.now,
            "meshRevision": self.snapshot.revision,
            "hosts": [_row("beta")],
        }
        inventory = _Inventory(response)
        received: list[float] = []

        def factory(*_args: object, **kwargs: object) -> _Inventory:
            received.append(float(kwargs["whole_deadline_seconds"]))
            return inventory

        refresh = RemoteRefresh(
            Config(),
            self.cache,
            mesh_adapter=self.adapter,
            inventory_factory=factory,  # type: ignore[arg-type]
        )
        with patch("rofi_tmux_plus.picker_model.time.monotonic", side_effect=(100.0, 104.0)):
            self.assertTrue(refresh.run(self.snapshot.revision))
        self.assertEqual(received, [11.0])
        self.assertLess(_REFRESH_HARD_DEADLINE_SECONDS, _REFRESH_STALL_SECONDS)


class PrivateCliTests(unittest.TestCase):
    def test_private_refresh_is_silent_and_private_json_entries_are_clean(self) -> None:
        refresh = MagicMock()
        refresh.run.return_value = True
        output = StringIO()
        with patch("rofi_tmux_plus.cli._refresh", return_value=refresh), redirect_stdout(output):
            self.assertEqual(cli.main(["_refresh", "--mesh-revision", "sha256:fixture"]), 0)
        self.assertEqual(output.getvalue(), "")
        refresh.run.assert_called_once_with("sha256:fixture")
        model = MagicMock()
        model.load.return_value.payload = {"schemaVersion": 1, "hosts": []}
        output = StringIO()
        with patch("rofi_tmux_plus.cli._picker_model", return_value=model), redirect_stdout(output):
            self.assertEqual(cli.main(["_picker-model", "--json", "--no-refresh"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"schemaVersion": 1, "hosts": []})
        refresh = MagicMock()
        refresh.status.return_value = {"state": "complete", "meshRevision": "sha256:fixture"}
        output = StringIO()
        with patch("rofi_tmux_plus.cli._refresh", return_value=refresh), redirect_stdout(output):
            self.assertEqual(
                cli.main(["_refresh-status", "--json", "--mesh-revision", "sha256:fixture"]),
                0,
            )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "schemaVersion": 1,
                "meshRevision": "sha256:fixture",
                "refresh": {"state": "complete", "meshRevision": "sha256:fixture"},
            },
        )
        refresh.status.assert_called_once_with("sha256:fixture")
