from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rofi_tmux_plus.bounded_process import run_bounded
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.inventory_service import InventoryService
from rofi_tmux_plus.mesh_adapter import (
    HostMeshAdapter,
    MeshHost,
    MeshPolicy,
    MeshRoute,
    MeshSnapshot,
    MeshStaleError,
)
from rofi_tmux_plus.remote_inventory import (
    RemoteInventory,
    build_remote_inventory_argv,
    parse_reached_marker,
    parse_remote_inventory,
)


def _completed(stdout: str, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fake"], code, stdout, stderr)


def _field(value: str, *, tmux_output: bool = True) -> str:
    raw = value.encode("utf-8") + (b"\n" if tmux_output else b"")
    return raw.hex()


def _hostname_record(name: str = "beta-native") -> str:
    return "H\t" + _field(name, tmux_output=False) + "\n"


def _domain_output(*, panes: bool = False, options: tuple[str, ...] = ()) -> str:
    lines = [
        _hostname_record().removesuffix("\n"),
        "G\t" + "\t".join((_field("/tmp/tmux"), _field("10"), _field("20"))),
    ]
    lines.append(
        "D\t"
        + "\t".join(
            (
                _field("$0", tmux_output=False),
                _field("11"),
                _field("hostile\tname\npath"),
                _field("12"),
                _field(""),
                _field("0"),
                _field("1"),
                _field("/tmp/a\tb\n"),
                _field("window"),
                _field("/tmp/current"),
                _field("1", tmux_output=False),
            )
        )
    )
    for option in options:
        lines.append(
            "O\t"
            + "\t".join(
                (
                    _field("$0", tmux_output=False),
                    _field(option, tmux_output=False),
                    _field("value"),
                )
            )
        )
    if panes:
        lines.append(
            "P\t"
            + "\t".join(
                (
                    _field("$0", tmux_output=False),
                    _field("%0", tmux_output=False),
                    _field("123"),
                    _field("/tmp/pane\tpath"),
                    _field("codex"),
                )
            )
        )
    return "\n".join(lines) + "\n"


class FakeAdapter:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []
        self.stale = False
        self.failure: ContractError | None = None

    def report_route(self, **kwargs: object) -> bool:
        self.reports.append(kwargs)
        if self.stale:
            raise MeshStaleError()
        if self.failure is not None:
            raise self.failure
        return True


class HostMeshAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "contracts"
            / "host-mesh-v1"
            / "fixtures"
            / "consumer-inventory-v1.json"
        )
        self.payload = json.loads(fixture.read_text(encoding="utf-8"))["mesh"]

    def test_missing_provider_is_the_only_local_fallback(self) -> None:
        adapter = HostMeshAdapter(which=lambda _name: None)
        self.assertIsNone(adapter.load())

    def test_valid_mesh_and_unknown_fields_are_accepted(self) -> None:
        payload = {**self.payload, "future": {"ignored": True}}
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake/rofi-ssh-plus",
            runner=lambda *_args, **_kwargs: _completed(json.dumps(payload)),
        )
        snapshot = adapter.load()
        assert snapshot is not None
        self.assertEqual(snapshot.revision, "sha256:fixture")
        self.assertEqual([host.host_id for host in snapshot.hosts], ["alpha", "beta", "gamma"])
        self.assertEqual(snapshot.local_host.aliases[0], "alpha")
        self.assertEqual(snapshot.resolve_host("BETA-NATIVE").host_id, "beta")

    def test_same_host_identity_overlap_is_allowed_but_cross_host_route_collision_is_not(
        self,
    ) -> None:
        payload = json.loads(json.dumps(self.payload))
        payload["hosts"][2]["aliases"].append("beta-vpn.test")
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake/rofi-ssh-plus",
            runner=lambda *_args, **_kwargs: _completed(json.dumps(payload)),
        )
        with self.assertRaisesRegex(ContractError, "ambiguous"):
            adapter.load()

    def test_nonzero_malformed_and_unsupported_provider_are_visible_failures(self) -> None:
        cases = [
            _completed("not json"),
            _completed(json.dumps({**self.payload, "schemaVersion": 2})),
            _completed(
                json.dumps({"schemaVersion": 1, "ok": False, "error": {"code": "invalid_config"}}),
                code=1,
            ),
        ]
        for response in cases:
            adapter = HostMeshAdapter(
                which=lambda _name: "/fake/rofi-ssh-plus",
                runner=lambda *_args, response=response, **_kwargs: response,
            )
            with self.assertRaises(ContractError):
                adapter.load()

    def test_report_stale_revision_is_preserved(self) -> None:
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake/rofi-ssh-plus",
            runner=lambda *_args, **_kwargs: _completed(
                json.dumps({"schemaVersion": 1, "ok": False, "error": {"code": "stale_mesh"}}),
                code=1,
            ),
        )
        with self.assertRaises(MeshStaleError):
            adapter.report_route(
                host_id="beta",
                route="beta.test",
                status="reachable",
                mesh_revision="sha256:x",
                observed_at=1,
            )

    def test_report_uses_only_the_public_command_with_exact_health_metadata(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return _completed(json.dumps({"schemaVersion": 1, "ok": True, "accepted": True}))

        adapter = HostMeshAdapter(which=lambda _name: "/fake/rofi-ssh-plus", runner=runner)
        self.assertTrue(
            adapter.report_route(
                host_id="beta",
                route="beta-vpn.test",
                status="reachable",
                mesh_revision="sha256:fixture",
                observed_at=1234,
            )
        )
        self.assertEqual(
            calls,
            [
                [
                    "/fake/rofi-ssh-plus",
                    "mesh",
                    "report-route",
                    "--json",
                    "--host",
                    "beta",
                    "--route",
                    "beta-vpn.test",
                    "--status",
                    "reachable",
                    "--source",
                    "rofi-tmux-plus",
                    "--mesh-revision",
                    "sha256:fixture",
                    "--observed-at",
                    "1234",
                ]
            ],
        )

    def test_copied_marker_fixture_has_exact_v1_parser_behavior(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "contracts"
            / "host-mesh-v1"
            / "fixtures"
            / "reached-markers.json"
        )
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            reached, remaining = parse_reached_marker(case["stderr"], payload["nonce"])
            self.assertEqual(reached, case["expectedReached"], case["name"])
            self.assertEqual(remaining, case["expectedRemainingStderr"], case["name"])


class BoundedProcessTests(unittest.TestCase):
    @staticmethod
    def _script(directory: Path, name: str, body: str) -> Path:
        path = directory / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    def test_timeout_is_terminated_and_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            script = self._script(Path(raw_directory), "hang", "while :; do :; done\n")
            started = time.monotonic()
            result = run_bounded([str(script)], timeout=0.05, stdout_limit=128, stderr_limit=128)
        self.assertTrue(result.timed_out)
        self.assertIsInstance(result.returncode, int)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_host_mesh_and_ssh_default_runners_reject_bounded_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            script = self._script(Path(raw_directory), "overflow", "yes x\n")
            adapter = HostMeshAdapter(which=lambda _name: str(script))
            with self.assertRaisesRegex(ContractError, "exceeded"):
                adapter.load()

            remote_adapter = FakeAdapter()
            remote = RemoteInventory(remote_adapter)
            host = MeshHost(
                "beta",
                "Beta",
                False,
                (),
                (MeshRoute("beta.test", 0, None, None),),
            )
            row = remote.inventory(
                host,
                MeshPolicy(str(script), 1, 1, 300),
                "sha256:fixture",
                panes=False,
                option_names=[],
                deadline=time.monotonic() + 1,
            )
        self.assertEqual(row["status"], "error")
        self.assertEqual(remote_adapter.reports, [])


class RemoteInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = MeshPolicy("ssh", 2, 1, 300)
        self.host = MeshHost(
            "beta",
            "Beta",
            False,
            ("beta-native",),
            (MeshRoute("beta-vpn.test", 0, None, None), MeshRoute("beta-lan.test", 1, None, None)),
        )
        self.adapter = FakeAdapter()
        self.nonce = "0123456789abcdef0123456789abcdef"

    def _inventory(self, responses: list[subprocess.CompletedProcess[str]]) -> RemoteInventory:
        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        return RemoteInventory(
            self.adapter, runner=runner, nonce_factory=lambda: self.nonce, now_millis=lambda: 1234
        )

    def test_valid_marker_reports_reachable_and_parses_hostile_fields_panes_options(self) -> None:
        remote = self._inventory(
            [
                _completed(
                    _domain_output(panes=True, options=("@codex_thread_id",)),
                    "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n",
                )
            ]
        )
        row = remote.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=True,
            option_names=["@codex_thread_id"],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["route"], "beta-vpn.test")
        session = row["sessions"][0]
        self.assertEqual(session["name"], "hostile\tname\npath")
        self.assertEqual(session["options"], {"@codex_thread_id": "value"})
        self.assertEqual(len(session["panes"]), 1)
        self.assertEqual(self.adapter.reports[0]["status"], "reachable")
        self.assertEqual(self.adapter.reports[0]["observed_at"], 1234)

    def test_transport_fallback_reports_each_attempt_and_wrong_or_duplicate_markers_do_not_count(
        self,
    ) -> None:
        valid = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        remote = self._inventory(
            [
                _completed("", "Connection refused\n", 255),
                _completed(_domain_output(), valid),
            ]
        )
        row = remote.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["route"], "beta-lan.test")
        self.assertEqual(
            [report["status"] for report in self.adapter.reports], ["unreachable", "reachable"]
        )
        self.assertEqual(
            [report["route"] for report in self.adapter.reports], ["beta-vpn.test", "beta-lan.test"]
        )
        duplicate = valid + valid
        self.adapter.reports.clear()
        remote = self._inventory(
            [_completed("", duplicate, 0), _completed(_domain_output(), valid)]
        )
        row = remote.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["route"], "beta-lan.test")
        self.assertEqual([report["status"] for report in self.adapter.reports], ["reachable"])

    def test_auth_host_key_and_wrong_marker_each_try_the_next_route_without_health_report(
        self,
    ) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        wrong = "\x1eROFI_PLUS_REACHED_V1:fedcba9876543210fedcba9876543210\x1f\n"
        for stderr, code in (
            ("Permission denied (publickey).\n", 255),
            ("Host key verification failed.\n", 255),
            (wrong, 0),
        ):
            with self.subTest(stderr=stderr):
                self.adapter.reports.clear()
                remote = self._inventory(
                    [_completed("", stderr, code), _completed(_domain_output(), marker)]
                )
                row = remote.inventory(
                    self.host,
                    self.policy,
                    "sha256:fixture",
                    panes=False,
                    option_names=[],
                    deadline=time.monotonic() + 5,
                )
                self.assertEqual(row["status"], "ok")
                self.assertEqual(row["route"], "beta-lan.test")
                self.assertEqual(
                    [report["status"] for report in self.adapter.reports], ["reachable"]
                )

    def test_all_route_exhaustion_is_unreachable_only_for_classified_transport_failures(
        self,
    ) -> None:
        transport = self._inventory(
            [
                _completed("", "Connection refused\n", 255),
                _completed("", "Network is unreachable\n", 255),
            ]
        )
        row = transport.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual([report["status"] for report in self.adapter.reports], ["unreachable"] * 2)

        self.adapter.reports.clear()
        mixed = self._inventory(
            [
                _completed("", "Connection refused\n", 255),
                _completed("", "Permission denied\n", 255),
            ]
        )
        row = mixed.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "error")
        self.assertEqual([report["status"] for report in self.adapter.reports], ["unreachable"])

    def test_domain_failure_after_marker_terminates_selection_and_report_stale_discards(
        self,
    ) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        remote = self._inventory([_completed("", marker, 42)])
        row = remote.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["route"], "beta-vpn.test")
        self.assertEqual(len(self.adapter.reports), 1)
        self.adapter.stale = True
        remote = self._inventory([_completed(_domain_output(), marker)])
        with self.assertRaises(MeshStaleError):
            remote.inventory(
                self.host,
                self.policy,
                "sha256:fixture",
                panes=False,
                option_names=[],
                deadline=time.monotonic() + 5,
            )

    def test_tmux_missing_no_server_running_empty_and_generic_error_are_distinct(self) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        for output, status, generation in (
            (_hostname_record() + "T\tM\n", "tmux_missing", None),
            (_hostname_record() + "T\tN\n", "ok", None),
            ("\n".join(_domain_output().splitlines()[:2]) + "\n", "ok", "tmux-v1:10:20:/tmp/tmux"),
        ):
            remote = self._inventory([_completed(output, marker)])
            row = remote.inventory(
                self.host,
                self.policy,
                "sha256:fixture",
                panes=False,
                option_names=[],
                deadline=time.monotonic() + 5,
            )
            self.assertEqual(row["status"], status)
            self.assertEqual(row["serverGeneration"], generation)
            self.assertEqual(row["nativeHostname"], "beta-native")
        generic = _hostname_record() + "E\t" + _field("tmux access denied") + "\n"
        remote = self._inventory([_completed(generic, marker)])
        row = remote.inventory(
            self.host,
            self.policy,
            "sha256:fixture",
            panes=False,
            option_names=[],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["nativeHostname"], "beta-native")
        self.assertEqual(row["error"]["message"], "tmux access denied")
        remote = self._inventory([_completed("D\tbad\n", marker)])
        self.assertEqual(
            remote.inventory(
                self.host,
                self.policy,
                "sha256:fixture",
                panes=False,
                option_names=[],
                deadline=time.monotonic() + 5,
            )["status"],
            "error",
        )

    def test_remote_parser_rejects_duplicate_status_and_oversized_numeric_fields(self) -> None:
        with self.assertRaises(ContractError):
            parse_remote_inventory(
                "T\tN\nT\tN\n", host_id="beta", panes_requested=False, option_names=[]
            )
        overflow = _domain_output().replace(_field("11"), _field(str(2**63)), 1)
        with self.assertRaises(ContractError):
            parse_remote_inventory(overflow, host_id="beta", panes_requested=False, option_names=[])

    def test_literal_hostile_route_stays_an_argv_element(self) -> None:
        hostile = "user@host;$(touch should-not-run)"
        argv = build_remote_inventory_argv(
            hostile, self.policy, panes=False, option_names=[], nonce=self.nonce
        )
        self.assertEqual(argv[-2], hostile)
        self.assertNotIn(hostile, argv[-1])
        reached, remaining = parse_reached_marker(
            "before\n\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\nafter\n", self.nonce
        )
        self.assertTrue(reached)
        self.assertEqual(remaining, "before\nafter\n")


class _LocalTmux:
    def inventory(
        self, _host_id: str, *, panes: bool, option_names: tuple[str, ...]
    ) -> tuple[None, list[object]]:
        return None, []


class _MeshProvider:
    def __init__(self, snapshot: MeshSnapshot | None) -> None:
        self.snapshot = snapshot

    def load(self) -> MeshSnapshot | None:
        return self.snapshot


class _RemoteRows:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.option_names: list[tuple[str, ...]] = []
        self.deadline_remaining: list[float] = []

    def inventory(self, host: MeshHost, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.calls.append(host.host_id)
        self.option_names.append(tuple(_kwargs["option_names"]))
        self.deadline_remaining.append(_kwargs["deadline"] - time.monotonic())
        if host.host_id == "gamma":
            raise ContractError("operation_failed", "fixture remote failure")
        return {
            "hostId": host.host_id,
            "display": host.display,
            "local": False,
            "status": "ok",
            "observedAt": 1,
            "nativeHostname": None,
            "serverGeneration": None,
            "route": host.routes[0].destination,
            "sessions": [],
        }


class InventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "contracts"
            / "host-mesh-v1"
            / "fixtures"
            / "consumer-inventory-v1.json"
        )
        payload = json.loads(fixture.read_text(encoding="utf-8"))["mesh"]
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake",
            runner=lambda *_args, **_kwargs: _completed(json.dumps(payload)),
        )
        snapshot = adapter.load()
        assert snapshot is not None
        self.snapshot = snapshot

    def test_mesh_selection_order_dedupe_revision_and_partial_remote_rows(self) -> None:
        remote = _RemoteRows()
        service = InventoryService(
            Config(),
            mesh_adapter=_MeshProvider(self.snapshot),
            local_tmux=_LocalTmux(),
            remote_inventory=remote,
        )
        result = service.inventory(
            requested_hosts=["gamma", "beta", "beta-native"],
            mesh_revision="sha256:fixture",
            panes=False,
            option_names=[],
        )
        self.assertEqual([row["hostId"] for row in result["hosts"]], ["beta", "gamma"])
        self.assertEqual([row["status"] for row in result["hosts"]], ["ok", "error"])
        self.assertEqual(remote.calls, ["beta", "gamma"])
        self.assertTrue(all(0 < remaining <= 8.1 for remaining in remote.deadline_remaining))
        calls_before_stale = list(remote.calls)
        with self.assertRaisesRegex(ContractError, "stale_mesh"):
            service.inventory(
                requested_hosts=[], mesh_revision="sha256:old", panes=False, option_names=[]
            )
        self.assertEqual(remote.calls, calls_before_stale)

    def test_local_mesh_order_and_option_requests_are_sets(self) -> None:
        remote = _RemoteRows()
        service = InventoryService(
            Config(),
            mesh_adapter=_MeshProvider(self.snapshot),
            local_tmux=_LocalTmux(),
            remote_inventory=remote,
        )
        result = service.inventory(
            requested_hosts=["beta", "alpha-native", "alpha", "beta-native"],
            mesh_revision="sha256:fixture",
            panes=False,
            option_names=["@state", "@state"],
        )
        self.assertEqual([row["hostId"] for row in result["hosts"]], ["alpha", "beta"])
        self.assertEqual(remote.calls, ["beta"])
        self.assertEqual(remote.option_names, [("@state",)])

    def test_missing_provider_uses_only_local_inventory(self) -> None:
        remote = _RemoteRows()
        service = InventoryService(
            Config(),
            mesh_adapter=_MeshProvider(None),
            local_tmux=_LocalTmux(),
            remote_inventory=remote,
        )
        result = service.inventory(
            requested_hosts=[], mesh_revision=None, panes=False, option_names=[]
        )
        self.assertIsNone(result["meshRevision"])
        self.assertEqual(len(result["hosts"]), 1)
        self.assertTrue(result["hosts"][0]["local"])
        self.assertEqual(remote.calls, [])

    def test_stale_report_discards_the_entire_inventory_response(self) -> None:
        class StaleRemote:
            def inventory(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                raise MeshStaleError()

        service = InventoryService(
            Config(),
            mesh_adapter=_MeshProvider(self.snapshot),
            local_tmux=_LocalTmux(),
            remote_inventory=StaleRemote(),
        )
        with self.assertRaises(MeshStaleError):
            service.inventory(
                requested_hosts=["beta"],
                mesh_revision="sha256:fixture",
                panes=False,
                option_names=[],
            )

    def test_stale_waits_for_started_workers_before_propagating(self) -> None:
        effects: list[str] = []
        started = threading.Event()

        class SlowStaleRemote:
            def inventory(
                self, host: MeshHost, *_args: object, **kwargs: object
            ) -> dict[str, object]:
                if host.host_id == "beta":
                    started.wait(timeout=0.2)
                    raise MeshStaleError()
                deadline = kwargs["deadline"]
                started.set()
                while time.monotonic() < deadline:
                    time.sleep(0.002)
                effects.append(host.host_id)
                return {
                    "hostId": host.host_id,
                    "display": host.display,
                    "local": False,
                    "status": "ok",
                    "observedAt": 1,
                    "nativeHostname": None,
                    "serverGeneration": None,
                    "route": host.routes[0].destination,
                    "sessions": [],
                }

        service = InventoryService(
            Config(),
            mesh_adapter=_MeshProvider(self.snapshot),
            local_tmux=_LocalTmux(),
            remote_inventory=SlowStaleRemote(),
            whole_deadline_seconds=0.08,
        )
        started_at = time.monotonic()
        with self.assertRaises(MeshStaleError):
            service.inventory(
                requested_hosts=["beta", "gamma"],
                mesh_revision="sha256:fixture",
                panes=False,
                option_names=[],
            )
        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertEqual(effects, ["gamma"])
        time.sleep(0.03)
        self.assertEqual(effects, ["gamma"])
