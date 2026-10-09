from __future__ import annotations

import errno
import json
import signal
import subprocess
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from reference_frontend.lifecycle import LocalLifecycle
from reference_frontend.viewer_service import (
    Viewer,
    ViewerInspection,
    ViewerTarget,
    _ObservationProcessIndex,
    _Proc,
    _read_metadata_detailed,
    _remote_attach_argv,
    close_viewer,
    effective_destroy_unattached,
    inspect_viewers,
    observe_local_viewers,
)

from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.host import local_host
from rofi_tmux_plus.model import Session, SessionReference


def _session(host_id: str = "local") -> Session:
    return Session(
        SessionReference(host_id, "tmux-v1:10:20:/tmp/default", "$7", 30),
        "fixture",
        31,
        31,
        0,
        False,
        1,
        "/tmp",
        "main",
        "/tmp",
    )


def _kitty_window(window_id: int, pid: int, title: str = "other") -> dict[str, object]:
    return {"id": window_id, "pid": pid, "app_id": "kitty", "title": title}


def _proc(pid: int, ppid: int, start: int, tty: int, argv: tuple[str, ...]) -> _Proc:
    return _Proc(pid, ppid, pid, 1, tty, start, argv)


def _observation_session(
    host_id: str = "alpha",
    *,
    session_id: str = "$7",
    name: str = "fixture",
    attached_clients: int | None = 0,
    pending: bool = False,
) -> Session:
    return Session(
        SessionReference(host_id, "tmux-v1:10:20:/tmp/default", session_id, 30),
        name,
        31,
        31,
        attached_clients,
        pending,
        1,
        "/tmp",
        "main",
        "/tmp",
    )


def _viewer(launch_id: str | None = "launch") -> Viewer:
    reference = _session().reference
    argv = ("tmux", "-u", "attach-session", "-t", "$7")
    return Viewer(
        "tv1_" + "a" * 43,
        101,
        10,
        100,
        20,
        200,
        launch_id,
        reference.as_dict(),
        argv,
        41,
    )


class DestroyUnattachedTests(unittest.TestCase):
    def test_session_override_and_global_fallback_are_distinguished(self) -> None:
        tmux = Mock()
        tmux.run.return_value = "@other value\ndestroy-unattached on\n"
        tmux.try_run.return_value = subprocess.CompletedProcess([], 0, "on\n", "")
        self.assertEqual(effective_destroy_unattached(tmux, "$7"), "on")
        tmux.try_run.assert_called_once_with(
            ["show-options", "-qv", "-t", "$7", "destroy-unattached"]
        )

        tmux.run.return_value = "@other value\n"
        tmux.try_run.return_value = subprocess.CompletedProcess([], 0, "off\n", "")
        self.assertEqual(effective_destroy_unattached(tmux, "$7"), "off")
        self.assertEqual(
            tmux.try_run.call_args.args[0], ["show-options", "-gqv", "destroy-unattached"]
        )

    def test_unreadable_effective_option_is_unsafe(self) -> None:
        tmux = Mock()
        tmux.run.return_value = ""
        tmux.try_run.return_value = subprocess.CompletedProcess([], 1, "", "failed")
        self.assertIsNone(effective_destroy_unattached(tmux, "$7"))


class LaunchMetadataTests(unittest.TestCase):
    def test_local_attachment_spawn_inherits_full_reference_and_launch_id(self) -> None:
        session = _session()
        lifecycle = LocalLifecycle(Mock(), Config(terminal=("kitty",)), host=local_host())
        with patch("reference_frontend.lifecycle.spawn_terminal_command") as spawn:
            launch_id = lifecycle._spawn_terminal(session)
        spawn.assert_called_once()
        command = spawn.call_args.args[1]
        environment = spawn.call_args.kwargs["env"]
        marker = json.loads(environment["ROFI_TMUX_PLUS_VIEWER_V1"])
        self.assertEqual(command, ["tmux", "-u", "attach-session", "-t", "$7"])
        self.assertEqual(marker["schemaVersion"], 1)
        self.assertEqual(marker["launchId"], launch_id)
        self.assertEqual(marker["sessionRef"], session.reference.as_dict())


class ViewerInspectionTests(unittest.TestCase):
    def _inspect(
        self,
        windows: list[object],
        *,
        roots: dict[int, _Proc],
        direct: dict[int, list[_Proc]],
        trees: dict[int, list[_Proc]],
        client_pids: list[int] = (),
        metadata: dict[int, tuple[dict[str, object] | None, bool]] | None = None,
        session: Session | None = None,
        remote_route: str | None = None,
        remote_native_hostname: str | None = None,
    ) -> ViewerInspection:
        selected = session or _session()
        client = Mock(client_pids=Mock(return_value=client_pids))
        metadata = metadata or {}

        def process(pid: int) -> _Proc | None:
            return roots.get(pid) or next(
                (item for rows in direct.values() for item in rows if item.pid == pid), None
            )

        with (
            patch("reference_frontend.viewer_service._niri_windows", return_value=windows),
            patch("reference_frontend.viewer_service._proc", side_effect=process),
            patch(
                "reference_frontend.viewer_service._direct_children",
                side_effect=lambda pid: direct.get(pid, []),
            ),
            patch(
                "reference_frontend.viewer_service._process_tree",
                side_effect=lambda pid: (trees.get(pid, []), True),
            ),
            patch(
                "reference_frontend.viewer_service._read_metadata",
                side_effect=lambda pid: metadata.get(pid, (None, False)),
            ),
        ):
            return inspect_viewers(
                selected,
                Config(terminal=("kitty",)),
                local_tmux=client if remote_route is None else None,
                remote_route=remote_route,
                remote_executable="ssh",
                remote_native_hostname=remote_native_hostname,
                destroy_unattached="off",
            )

    def test_local_legacy_adoption_joins_exact_tmux_client_pid(self) -> None:
        session = _session()
        root = _proc(10, 1, 100, 0, ("kitty",))
        client = _proc(20, 10, 200, 41, ("tmux", "-u", "attach-session", "-t", "$7"))
        helper = _proc(21, 10, 201, 0, ("kitten", "+kitten", "clipboard"))
        inspection = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [client, helper]},
            trees={10: [client, helper]},
            client_pids=[20],
            session=session,
        )
        self.assertEqual(inspection.status, "verified")
        self.assertEqual(len(inspection.viewers), 1)
        self.assertTrue(inspection.viewers[0].viewer_id.startswith("tv1_"))

    def test_unrelated_multi_pty_kitty_does_not_make_target_ambiguous(self) -> None:
        root = _proc(10, 1, 100, 0, ("kitty",))
        first = _proc(20, 10, 200, 41, ("zsh",))
        second = _proc(21, 10, 201, 42, ("zsh",))
        inspection = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [first, second]},
            trees={10: [first, second]},
        )
        self.assertEqual(inspection.status, "none")

    def test_target_multi_pty_layout_is_ambiguous(self) -> None:
        root = _proc(10, 1, 100, 0, ("kitty",))
        client = _proc(20, 10, 200, 41, ("tmux", "-u", "attach-session", "-t", "$7"))
        second = _proc(21, 10, 201, 42, ("zsh",))
        inspection = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [client, second]},
            trees={10: [client, second]},
            client_pids=[20],
        )
        self.assertEqual(inspection.status, "ambiguous")
        self.assertEqual(inspection.viewers, ())

    def test_exact_local_attachment_without_current_client_pid_is_unverified(self) -> None:
        session = _session()
        root = _proc(10, 1, 100, 0, ("kitty",))
        client = _proc(20, 10, 200, 41, ("tmux", "-u", "attach-session", "-t", "$7"))
        legacy = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [client]},
            trees={10: [client]},
            client_pids=[],
            session=session,
        )
        self.assertEqual(legacy.status, "unverified")
        self.assertEqual(legacy.viewers, ())

        metadata = {
            10: (
                {
                    "schemaVersion": 1,
                    "launchId": "launch",
                    "sessionRef": session.reference.as_dict(),
                },
                True,
            )
        }
        marked = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [client]},
            trees={10: [client]},
            client_pids=[],
            metadata=metadata,
            session=session,
        )
        self.assertEqual(marked.status, "unverified")
        self.assertEqual(marked.pending_launch_ids, ())

    def test_remote_marked_direct_ssh_child_is_verified_and_legacy_is_not(self) -> None:
        session = _session("starship")
        root = _proc(10, 1, 100, 0, ("kitty",))
        argv = _remote_attach_argv("ssh", "starship", "$7")
        ssh = _proc(20, 10, 200, 41, argv)
        metadata = {
            10: (
                {
                    "schemaVersion": 1,
                    "launchId": "launch",
                    "sessionRef": session.reference.as_dict(),
                },
                True,
            )
        }
        verified = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [ssh]},
            trees={10: [ssh]},
            metadata=metadata,
            session=session,
            remote_route="starship",
            remote_native_hostname="starship",
        )
        self.assertEqual(verified.status, "verified")
        legacy = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: [ssh]},
            trees={10: [ssh]},
            session=session,
            remote_route="starship",
            remote_native_hostname="starship",
        )
        self.assertEqual(legacy.status, "unverified")
        self.assertEqual(legacy.viewers, ())

    def test_manual_remote_shell_title_does_not_supply_close_handles(self) -> None:
        session = _session("starship")
        root = _proc(10, 1, 100, 0, ("kitty",))
        ssh = _proc(20, 10, 200, 41, ("ssh", "starship"))
        result = self._inspect(
            [_kitty_window(101, 10, "fixture: task @ starship")],
            roots={10: root},
            direct={10: [ssh]},
            trees={10: [ssh]},
            session=session,
            remote_route="starship",
            remote_native_hostname="starship",
        )
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.viewers, ())

    def test_remote_marked_window_reports_registration_pending_until_attach(self) -> None:
        session = _session("starship")
        root = _proc(10, 1, 100, 0, ("kitty",))
        metadata = {
            10: (
                {
                    "schemaVersion": 1,
                    "launchId": "launch",
                    "sessionRef": session.reference.as_dict(),
                },
                True,
            )
        }
        inspection = self._inspect(
            [_kitty_window(101, 10)],
            roots={10: root},
            direct={10: []},
            trees={10: []},
            metadata=metadata,
            session=session,
            remote_route="starship",
            remote_native_hostname="starship",
        )
        self.assertEqual(inspection.status, "none")
        self.assertEqual(inspection.pending_launch_ids, ("launch",))

    def test_multiple_separate_viewers_are_verified_but_shared_pid_is_ambiguous(self) -> None:
        session = _session()
        roots = {10: _proc(10, 1, 100, 0, ("kitty",)), 11: _proc(11, 1, 101, 0, ("kitty",))}
        clients = {
            10: [_proc(20, 10, 200, 41, ("tmux", "-u", "attach-session", "-t", "$7"))],
            11: [_proc(21, 11, 201, 42, ("tmux", "-u", "attach-session", "-t", "$7"))],
        }
        metadata = {
            10: (
                {
                    "schemaVersion": 1,
                    "launchId": "first",
                    "sessionRef": session.reference.as_dict(),
                },
                True,
            ),
            11: (
                {
                    "schemaVersion": 1,
                    "launchId": "second",
                    "sessionRef": session.reference.as_dict(),
                },
                True,
            ),
        }
        rows = [_kitty_window(101, 10), _kitty_window(102, 11)]
        verified = self._inspect(
            rows,
            roots=roots,
            direct=clients,
            trees=clients,
            client_pids=[20, 21],
            metadata=metadata,
        )
        self.assertEqual(verified.status, "verified")
        self.assertEqual(len(verified.viewers), 2)

        shared = self._inspect(
            [_kitty_window(101, 10), _kitty_window(102, 10)],
            roots={10: roots[10]},
            direct={10: clients[10]},
            trees={10: clients[10]},
            client_pids=[20],
            metadata={10: metadata[10]},
        )
        self.assertEqual(shared.status, "ambiguous")
        self.assertEqual(shared.viewers, ())

    def test_matching_non_kitty_terminal_blocks_duplicate_but_unrelated_is_ignored(self) -> None:
        root = _proc(10, 1, 100, 0, ("ghostty",))
        client = _proc(20, 10, 200, 41, ("tmux", "-u", "attach-session", "-t", "$7"))
        matched = {
            "id": 101,
            "pid": 10,
            "app_id": "com.mitchellh.ghostty",
            "title": "fixture: task",
        }
        inspection = self._inspect(
            [matched],
            roots={10: root},
            direct={10: [client]},
            trees={10: [client]},
            client_pids=[20],
        )
        self.assertEqual(inspection.status, "unsupported")

        # A proven Kitty viewer cannot hide another unsupported attachment
        # from the preview for the same session.
        kitty_root = _proc(11, 1, 101, 0, ("kitty",))
        kitty_client = _proc(21, 11, 201, 42, ("tmux", "-u", "attach-session", "-t", "$7"))
        mixed = self._inspect(
            [matched, _kitty_window(102, 11)],
            roots={10: root, 11: kitty_root},
            direct={10: [client], 11: [kitty_client]},
            trees={10: [client], 11: [kitty_client]},
            client_pids=[20, 21],
        )
        self.assertEqual(mixed.status, "unsupported")
        self.assertEqual(mixed.viewers, ())

        unrelated = {"id": 102, "pid": 10, "app_id": "com.mitchellh.ghostty", "title": "other"}
        ignored = self._inspect(
            [unrelated],
            roots={10: root},
            direct={10: [client]},
            trees={10: [client]},
            client_pids=[],
        )
        self.assertEqual(ignored.status, "none")


class LocalViewerObservationTests(unittest.TestCase):
    def _observe(
        self,
        windows: list[object],
        processes: dict[int, _Proc],
        targets: tuple[ViewerTarget, ...],
        *,
        clients: dict[str, set[int]] | None = None,
        metadata: dict[int, tuple[dict[str, object] | None, bool, bool]] | None = None,
        children: dict[int, bytes] | None = None,
        unreadable_children: set[int] | None = None,
    ) -> tuple[object, Mock, Mock, list[int], list[int]]:
        tmux = Mock()
        tmux.client_pids_by_session.return_value = {} if clients is None else clients
        process_reads: list[int] = []
        child_reads: list[int] = []
        metadata_rows = metadata or {}
        child_rows = children or {}
        unreadable = unreadable_children or set()

        def read_process(pid: int) -> _Proc | None:
            process_reads.append(pid)
            return processes.get(pid)

        def read_children(path: Path) -> bytes:
            pid = int(path.parts[2])
            child_reads.append(pid)
            if pid in unreadable:
                raise PermissionError("fixture process tree is unreadable")
            return child_rows.get(pid, b"")

        with (
            patch("reference_frontend.viewer_service._niri_windows", return_value=windows) as niri,
            patch("reference_frontend.viewer_service._proc", side_effect=read_process),
            patch(
                "reference_frontend.viewer_service.Path.read_bytes",
                autospec=True,
                side_effect=read_children,
            ),
            patch(
                "reference_frontend.viewer_service._read_metadata_detailed",
                side_effect=lambda pid: metadata_rows.get(pid, (None, False, True)),
            ),
        ):
            batch = observe_local_viewers(
                targets,
                Config(terminal=("kitty",)),
                local_tmux=tmux,
                now_millis=lambda: 1_234,
            )
        self.assertEqual(niri.call_count, 1)
        return batch, tmux, niri, process_reads, child_reads

    @staticmethod
    def _target(session: Session, *, local: bool, route: str | None = None) -> ViewerTarget:
        return ViewerTarget(
            session,
            local,
            "alpha-native" if local else "beta-native.example",
            route,
            "ssh",
        )

    @staticmethod
    def _marked(session: Session) -> tuple[dict[str, object] | None, bool, bool]:
        return (
            {
                "schemaVersion": 1,
                "launchId": "fixture-launch",
                "sessionRef": session.reference.as_dict(),
            },
            True,
            True,
        )

    def test_local_launch_marker_and_old_attach_argv_cannot_override_current_join(self) -> None:
        original = _observation_session(session_id="$7")
        current = _observation_session(session_id="$8")
        targets = (
            self._target(original, local=True),
            self._target(current, local=True),
        )
        batch, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, ("tmux", "attach-session", "-t", "$7")),
            },
            targets,
            clients={"$8": {20}},
            metadata={10: self._marked(original)},
            children={10: b"20"},
        )
        observations = batch.observations
        self.assertEqual(
            observations[original.reference].as_dict(),
            {
                "state": "unknown",
                "reason": "conflicting_metadata",
            },
        )
        self.assertEqual(
            observations[current.reference].as_dict(),
            {
                "state": "open",
                "confidence": "confirmed",
            },
        )

    def test_current_local_pid_join_confirms_nonliteral_attach_argv(self) -> None:
        session = _observation_session()
        target = self._target(session, local=True)
        batch, tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 0, ("tmux", "-C")),
            },
            (target,),
            clients={"$7": {20}},
            children={10: b"20"},
        )
        self.assertEqual(
            batch.observations[session.reference].as_dict(),
            {
                "state": "open",
                "confidence": "confirmed",
            },
        )
        tmux.run.assert_not_called()

    def test_observation_can_confirm_presence_without_a_close_safe_layout_or_handle(self) -> None:
        session = _observation_session(attached_clients=0)
        target = self._target(session, local=True)
        batch, tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, ("tmux", "attach-session", "-t", "$7")),
                21: _proc(21, 10, 201, 42, ("zsh",)),
            },
            (target,),
            clients={"$7": {20}},
            children={10: b"20 21"},
        )
        self.assertEqual(
            batch.observations[session.reference].as_dict(),
            {"state": "open", "confidence": "confirmed"},
        )
        self.assertNotIn("viewerId", batch.observations[session.reference].as_dict())
        tmux.run.assert_not_called()

    def test_marked_remote_is_confirmed_and_qualified_legacy_remote_is_matched(self) -> None:
        session = _observation_session("beta")
        target = self._target(session, local=False, route="beta")
        remote_argv = _remote_attach_argv("ssh", "beta", "$7")
        processes = {
            10: _proc(10, 1, 100, 0, ("kitty",)),
            20: _proc(20, 10, 200, 41, remote_argv),
        }
        window = _kitty_window(101, 10, "fixture: task @ beta-native")
        marked, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [window],
            processes,
            (target,),
            metadata={10: self._marked(session)},
            children={10: b"20"},
        )
        self.assertEqual(
            marked.observations[session.reference].as_dict(),
            {
                "state": "open",
                "confidence": "confirmed",
            },
        )
        legacy, tmux, _niri, _process_reads, _child_reads = self._observe(
            [window], processes, (target,), children={10: b"20"}
        )
        self.assertEqual(
            legacy.observations[session.reference].as_dict(),
            {
                "state": "open",
                "confidence": "matched",
            },
        )
        tmux.client_pids_by_session.assert_not_called()

    def test_manual_remote_shell_with_unique_owner_title_is_only_matched(self) -> None:
        session = _observation_session("beta", attached_clients=1)
        other = _observation_session("beta", session_id="$8", name="other", attached_clients=1)
        targets = tuple(self._target(row, local=False, route="beta") for row in (session, other))
        for argv in (("ssh", "beta"), ("/usr/bin/ssh", "-t", "beta"), ("ssh", "-tt", "beta")):
            with self.subTest(argv=argv):
                batch, tmux, _niri, _process_reads, _child_reads = self._observe(
                    [_kitty_window(101, 10, "fixture: task @ beta-native")],
                    {
                        10: _proc(10, 1, 100, 0, ("kitty",)),
                        20: _proc(20, 10, 200, 41, ("zsh",)),
                        30: _proc(30, 20, 300, 41, argv),
                    },
                    targets,
                    children={10: b"20", 20: b"30"},
                )
                self.assertEqual(
                    batch.observations[session.reference].as_dict(),
                    {"state": "open", "confidence": "matched"},
                )
                self.assertEqual(batch.observations[other.reference].as_dict(), {"state": "none"})
                tmux.client_pids_by_session.assert_not_called()
                tmux.run.assert_not_called()

    def test_manual_shell_requires_peer_process_title_and_owner_attachment(self) -> None:
        cases = (
            (("ssh", "other"), 41, 1, "fixture: task @ beta-native"),
            (("ssh", "beta", "sleep 60"), 41, 1, "fixture: task @ beta-native"),
            (("zsh",), 41, 1, "fixture: task @ beta-native"),
            (("ssh", "beta"), 0, 1, "fixture: task @ beta-native"),
            (("ssh", "beta"), 41, 0, "fixture: task @ beta-native"),
            (("ssh", "beta"), 41, None, "fixture: task @ beta-native"),
            (("ssh", "beta"), 41, 1, "other: task @ beta-native"),
            (("ssh", "beta"), 41, 1, "fixture: task @ other-native"),
        )
        for argv, tty, clients, title in cases:
            with self.subTest(argv=argv, tty=tty, clients=clients, title=title):
                session = _observation_session("beta", attached_clients=clients)
                batch, _tmux, _niri, _process_reads, _child_reads = self._observe(
                    [_kitty_window(101, 10, title)],
                    {
                        10: _proc(10, 1, 100, 0, ("kitty",)),
                        20: _proc(20, 10, 200, tty, argv),
                    },
                    (self._target(session, local=False, route="beta"),),
                    children={10: b"20"},
                )
                self.assertNotEqual(batch.observations[session.reference].state, "open")

    def test_manual_shell_cannot_override_conflicts_partial_scan_or_pending(self) -> None:
        session = _observation_session("beta", attached_clients=1)
        other = _observation_session("beta", session_id="$8", attached_clients=1)
        pending = _observation_session("beta", attached_clients=1, pending=True)
        window = _kitty_window(101, 10, "fixture: task @ beta-native")
        cases = (
            (session, {"metadata": {10: self._marked(other)}}, "conflicting_metadata"),
            (session, {"metadata": {10: (None, True, True)}}, "conflicting_metadata"),
            (session, {"metadata": {10: (None, False, False)}}, "process_unavailable"),
            (session, {"unreadable_children": {20}}, "process_unavailable"),
            (pending, {}, "pending_registration"),
        )
        for selected, extra, reason in cases:
            with self.subTest(reason=reason):
                batch, _tmux, _niri, _process_reads, _child_reads = self._observe(
                    [window],
                    {
                        10: _proc(10, 1, 100, 0, ("kitty",)),
                        20: _proc(20, 10, 200, 41, ("ssh", "beta")),
                    },
                    (self._target(selected, local=False, route="beta"),),
                    children={10: b"20"},
                    **extra,
                )
                self.assertEqual(
                    batch.observations[selected.reference].as_dict(),
                    {"state": "unknown", "reason": reason},
                )
        duplicate, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [window, _kitty_window(102, 11, "fixture: task @ beta-native")],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                11: _proc(11, 1, 101, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, ("ssh", "beta")),
                21: _proc(21, 11, 201, 42, ("ssh", "beta")),
            },
            (self._target(session, local=False, route="beta"),),
            children={10: b"20", 11: b"21"},
        )
        self.assertEqual(
            duplicate.observations[session.reference].as_dict(),
            {"state": "unknown", "reason": "ambiguous_match"},
        )

    def test_global_remote_client_count_does_not_imply_a_local_viewer(self) -> None:
        session = _observation_session("beta", attached_clients=4)
        target = self._target(session, local=False, route="beta")
        batch, _tmux, _niri, _process_reads, _child_reads = self._observe([], {}, (target,))
        self.assertEqual(batch.observations[session.reference].as_dict(), {"state": "none"})

    def test_title_collision_and_contradictory_metadata_are_unknown(self) -> None:
        first = _observation_session("beta", name="same")
        second = _observation_session("gamma", name="same")
        first_target = self._target(first, local=False, route="beta")
        second_target = ViewerTarget(second, False, "beta-native.example", "gamma", "ssh")
        collision, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10, "same: task @ beta-native")],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, _remote_attach_argv("ssh", "beta", "$7")),
            },
            (first_target, second_target),
            children={10: b"20"},
        )
        for session in (first, second):
            self.assertEqual(
                collision.observations[session.reference].as_dict(),
                {"state": "unknown", "reason": "ambiguous_match"},
            )

        marked_other = _observation_session("beta", session_id="$8")
        contradictory, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, _remote_attach_argv("ssh", "beta", "$7")),
            },
            (first_target,),
            metadata={10: self._marked(marked_other)},
            children={10: b"20"},
        )
        self.assertEqual(
            contradictory.observations[first.reference].as_dict(),
            {"state": "unknown", "reason": "conflicting_metadata"},
        )

    def test_unreadable_process_or_metadata_and_pending_registration_fail_closed(self) -> None:
        session = _observation_session("beta")
        target = self._target(session, local=False, route="beta")
        unreadable, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {10: _proc(10, 1, 100, 0, ("kitty",))},
            (target,),
            unreadable_children={10},
        )
        self.assertEqual(
            unreadable.observations[session.reference].as_dict(),
            {
                "state": "unknown",
                "reason": "process_unavailable",
            },
        )

        metadata_unreadable, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {
                10: _proc(10, 1, 100, 0, ("kitty",)),
                20: _proc(20, 10, 200, 41, _remote_attach_argv("ssh", "beta", "$7")),
            },
            (target,),
            metadata={10: (None, False, False)},
            children={10: b"20"},
        )
        self.assertEqual(
            metadata_unreadable.observations[session.reference].as_dict(),
            {
                "state": "unknown",
                "reason": "process_unavailable",
            },
        )

        pending, _tmux, _niri, _process_reads, _child_reads = self._observe(
            [_kitty_window(101, 10)],
            {10: _proc(10, 1, 100, 0, ("kitty",))},
            (target,),
            metadata={10: self._marked(session)},
            children={10: b""},
        )
        self.assertEqual(
            pending.observations[session.reference].as_dict(),
            {
                "state": "unknown",
                "reason": "pending_registration",
            },
        )

    def test_boolean_launch_schema_version_is_rejected(self) -> None:
        raw = b'ROFI_TMUX_PLUS_VIEWER_V1={"schemaVersion":true}\0'
        with (
            patch("reference_frontend.viewer_service.os.open", return_value=55),
            patch("reference_frontend.viewer_service.os.read", return_value=raw),
            patch("reference_frontend.viewer_service.os.close"),
        ):
            value, present, readable = _read_metadata_detailed(123)
        self.assertIsNone(value)
        self.assertTrue(present)
        self.assertTrue(readable)

    def test_budget_is_shared_and_never_caches_past_cap_or_deadline(self) -> None:
        with patch("reference_frontend.viewer_service._proc", return_value=None) as read_proc:
            capped = _ObservationProcessIndex(process_limit=2)
            self.assertIsNone(capped.proc(10))
            self.assertIsNone(capped.proc(11))
            self.assertIsNone(capped.proc(12))
            self.assertIsNone(capped.proc(13))
        self.assertEqual(len(capped.processes), 2)
        self.assertEqual(read_proc.call_count, 2)

        shared = _ObservationProcessIndex(process_limit=1)
        with (
            patch(
                "reference_frontend.viewer_service._proc",
                return_value=_proc(10, 1, 100, 0, ("kitty",)),
            ),
            patch("reference_frontend.viewer_service._read_metadata_detailed") as read_metadata,
        ):
            shared.proc(10)
            result = shared.metadata_state(10)
        self.assertEqual(result.status, "unreadable")
        self.assertFalse(shared.metadata)
        read_metadata.assert_not_called()

        expired = _ObservationProcessIndex(deadline=0)
        with patch("reference_frontend.viewer_service._proc") as read_expired:
            self.assertIsNone(expired.proc(10))
            self.assertIsNone(expired.proc(11))
        self.assertFalse(expired.processes)
        read_expired.assert_not_called()


class ExactCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        # Synthetic baseline policy tests also run on builds without pidfd.
        # Installed native close acceptance uses Starship's actual bindings.
        for name in (
            "reference_frontend.viewer_service.os.pidfd_open",
            "reference_frontend.viewer_service.signal.pidfd_send_signal",
        ):
            capability = patch(name, create=True)
            capability.start()
            self.addCleanup(capability.stop)
        self.viewer = _viewer()
        self.window = _kitty_window(101, 10)
        self.processes = {
            10: _proc(10, 1, 100, 0, ("kitty",)),
            20: _proc(20, 10, 200, 41, self.viewer.attachment_argv),
        }

    def test_pidfd_signals_only_exact_attachment_and_verifies_exit(self) -> None:
        verified = ViewerInspection("verified", (self.viewer,), True)
        with (
            patch("reference_frontend.viewer_service.os.pidfd_open", return_value=90) as open_pidfd,
            patch("reference_frontend.viewer_service.os.close"),
            patch("reference_frontend.viewer_service.signal.pidfd_send_signal") as send_signal,
            patch("reference_frontend.viewer_service._proc", side_effect=self.processes.get),
            patch(
                "reference_frontend.viewer_service._niri_windows",
                side_effect=[[self.window], []],
            ),
        ):
            result = close_viewer(
                self.viewer,
                revalidate=lambda: verified,
                validate_session=lambda: True,
            )
        self.assertTrue(result)
        open_pidfd.assert_called_once_with(20, 0)
        send_signal.assert_called_once_with(90, signal.SIGTERM, None, 0)

    def test_absent_handle_is_idempotent_and_never_opens_pidfd(self) -> None:
        with patch("reference_frontend.viewer_service.os.pidfd_open") as open_pidfd:
            result = close_viewer(
                self.viewer,
                revalidate=lambda: ViewerInspection("none", (), True),
                validate_session=lambda: True,
            )
        self.assertFalse(result)
        open_pidfd.assert_not_called()

    def test_frozen_handle_cannot_close_a_later_viewer_for_the_same_session(self) -> None:
        later = Viewer(
            "tv1_" + "b" * 43,
            102,
            11,
            101,
            21,
            201,
            "later-launch",
            self.viewer.session_ref,
            self.viewer.attachment_argv,
            self.viewer.tty_nr,
        )
        with patch("reference_frontend.viewer_service.os.pidfd_open") as open_pidfd:
            result = close_viewer(
                self.viewer,
                revalidate=lambda: ViewerInspection("verified", (later,), True),
                validate_session=lambda: True,
            )
        self.assertFalse(result)
        open_pidfd.assert_not_called()

    def test_changed_layout_and_unsafe_guard_refuse_close(self) -> None:
        with self.assertRaises(ContractError) as ambiguous:
            close_viewer(
                self.viewer,
                revalidate=lambda: ViewerInspection("ambiguous", (), True),
                validate_session=lambda: True,
            )
        self.assertEqual(ambiguous.exception.code, "viewer_ambiguous")
        with self.assertRaises(ContractError) as unsafe:
            close_viewer(
                self.viewer,
                revalidate=lambda: ViewerInspection("verified", (self.viewer,), False),
                validate_session=lambda: True,
            )
        self.assertEqual(unsafe.exception.code, "viewer_destroy_guard")

        for status, code in (
            ("unverified", "viewer_unverified"),
            ("unsupported", "viewer_unsupported"),
        ):
            with self.subTest(status=status), self.assertRaises(ContractError) as raised:
                close_viewer(
                    self.viewer,
                    revalidate=lambda status=status: ViewerInspection(status, (), True),
                    validate_session=lambda: True,
                )
            self.assertEqual(raised.exception.code, code)

    def test_pidfd_permission_failure_is_not_reported_as_already_closed(self) -> None:
        with (
            patch(
                "reference_frontend.viewer_service.os.pidfd_open",
                side_effect=OSError(errno.EPERM, "permission denied"),
            ),
            self.assertRaises(ContractError) as raised,
        ):
            close_viewer(
                self.viewer,
                revalidate=lambda: ViewerInspection("verified", (self.viewer,), True),
                validate_session=lambda: True,
            )
        self.assertEqual(raised.exception.code, "viewer_close_ambiguous")


class StrictOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = local_host()
        self.reference = SessionReference(self.host.host_id, "generation", "$7", 30)
        self.session = _session()
        self.session = Session(
            self.reference,
            self.session.name,
            self.session.activity_at,
            self.session.last_attached_at,
            self.session.attached_clients,
            self.session.pending,
            self.session.window_count,
            self.session.session_path,
            self.session.current_window,
            self.session.current_path,
        )
        self.options: dict[str, str] = {}
        self.tmux = Mock()
        self.tmux.find.side_effect = lambda reference: (
            self.session if reference == self.reference else None
        )
        self.tmux.option.side_effect = lambda _sid, name: self.options.get(name)
        self.launched: list[str] = []
        self.lifecycle = LocalLifecycle(
            self.tmux,
            Config(terminal=("kitty",)),
            host=self.host,
            terminal_spawner=self.launched.append,
        )
        self.lifecycle._destroy_unattached = lambda _session_id: "off"

    def _open(self, required_options: tuple[tuple[str, str], ...] = ()) -> dict[str, object]:
        return self.lifecycle.open(
            self.host.host_id,
            None,
            self.reference.server_generation,
            self.reference.session_id,
            self.reference.created_at,
            "fixture",
            required_options,
            verified_viewer=True,
        )

    def test_strict_open_focuses_verified_existing_window_without_launch(self) -> None:
        viewer = _viewer()
        with (
            patch("reference_frontend.lifecycle.local_mutation_lock", return_value=nullcontext()),
            patch(
                "reference_frontend.lifecycle.inspect_viewers",
                return_value=ViewerInspection("verified", (viewer,), True),
            ),
            patch("reference_frontend.lifecycle.focus_window", return_value=True) as focus,
        ):
            response = self._open()
        focus.assert_called_once_with(viewer.window_id, niri_command=("niri",))
        self.assertEqual(response["viewerId"], viewer.viewer_id)
        self.assertTrue(response["focused"])
        self.assertFalse(self.launched)

    def test_strict_open_launches_then_returns_only_its_registered_handle(self) -> None:
        viewer = _viewer()
        viewer = Viewer(
            viewer.viewer_id,
            viewer.window_id,
            viewer.window_pid,
            viewer.window_start,
            viewer.attachment_pid,
            viewer.attachment_start,
            "expected-launch",
            viewer.session_ref,
            viewer.attachment_argv,
            viewer.tty_nr,
        )
        states = [
            ViewerInspection("none", (), True),
            ViewerInspection("none", (), True, pending_launch_ids=("expected-launch",)),
            ViewerInspection("verified", (viewer,), True),
        ]
        with (
            patch("reference_frontend.lifecycle.local_mutation_lock", return_value=nullcontext()),
            patch(
                "reference_frontend.lifecycle.launch_metadata",
                return_value=("expected-launch", {"ROFI_TMUX_PLUS_VIEWER_V1": "{}"}),
            ),
            patch("reference_frontend.lifecycle.inspect_viewers", side_effect=states),
            patch("reference_frontend.lifecycle.time.sleep"),
        ):
            response = self._open()
        self.assertEqual(self.launched, ["$7"])
        self.assertEqual(response["viewerId"], viewer.viewer_id)
        self.assertFalse(response["focused"])
        self.assertTrue(response["terminalLaunched"])

    def test_strict_open_refuses_ambiguous_unverified_and_unsupported_candidates(self) -> None:
        for status, expected_code in (
            ("ambiguous", "viewer_ambiguous"),
            ("unverified", "viewer_unverified"),
        ):
            self.launched.clear()
            with (
                patch(
                    "reference_frontend.lifecycle.local_mutation_lock", return_value=nullcontext()
                ),
                patch(
                    "reference_frontend.lifecycle.inspect_viewers",
                    return_value=ViewerInspection(status, (), True),
                ),
                self.assertRaises(ContractError) as raised,
            ):
                self._open()
            self.assertEqual(raised.exception.code, expected_code)
            self.assertFalse(self.launched)

        unsupported = LocalLifecycle(
            self.tmux,
            Config(terminal=("ghostty",)),
            host=self.host,
            terminal_spawner=self.launched.append,
        )
        with (
            patch("reference_frontend.lifecycle.local_mutation_lock", return_value=nullcontext()),
            self.assertRaises(ContractError) as raised,
        ):
            unsupported.open(
                self.host.host_id,
                None,
                self.reference.server_generation,
                self.reference.session_id,
                self.reference.created_at,
                "fixture",
                verified_viewer=True,
            )
        self.assertEqual(raised.exception.code, "viewer_unsupported")
        self.assertFalse(self.launched)

    def test_full_reference_and_required_option_guards_precede_launch(self) -> None:
        self.options["@provider"] = "expected"
        with (
            patch("reference_frontend.lifecycle.local_mutation_lock", return_value=nullcontext()),
            self.assertRaises(ContractError) as raised,
        ):
            self._open((("@provider", "wrong"),))
        self.assertEqual(raised.exception.code, "stale_session")
        self.assertFalse(self.launched)

        self.tmux.find.side_effect = ContractError("stale_session", "full reference changed")
        with self.assertRaises(ContractError) as stale_reference:
            self.lifecycle.open(
                self.host.host_id,
                None,
                self.reference.server_generation,
                self.reference.session_id,
                self.reference.created_at + 1,
                "fixture",
                verified_viewer=True,
            )
        self.assertEqual(stale_reference.exception.code, "stale_session")
        self.assertFalse(self.launched)
