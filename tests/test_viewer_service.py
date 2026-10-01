from __future__ import annotations

import errno
import json
import signal
import subprocess
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.host import local_host
from rofi_tmux_plus.lifecycle import LocalLifecycle
from rofi_tmux_plus.model import Session, SessionReference
from rofi_tmux_plus.viewer_service import (
    Viewer,
    ViewerInspection,
    _Proc,
    _remote_attach_argv,
    close_viewer,
    effective_destroy_unattached,
    inspect_viewers,
)


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
        with patch("rofi_tmux_plus.lifecycle.spawn_terminal_command") as spawn:
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
            patch("rofi_tmux_plus.viewer_service._niri_windows", return_value=windows),
            patch("rofi_tmux_plus.viewer_service._proc", side_effect=process),
            patch(
                "rofi_tmux_plus.viewer_service._direct_children",
                side_effect=lambda pid: direct.get(pid, []),
            ),
            patch(
                "rofi_tmux_plus.viewer_service._process_tree",
                side_effect=lambda pid: (trees.get(pid, []), True),
            ),
            patch(
                "rofi_tmux_plus.viewer_service._read_metadata",
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


class ExactCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.viewer = _viewer()
        self.window = _kitty_window(101, 10)
        self.processes = {
            10: _proc(10, 1, 100, 0, ("kitty",)),
            20: _proc(20, 10, 200, 41, self.viewer.attachment_argv),
        }

    def test_pidfd_signals_only_exact_attachment_and_verifies_exit(self) -> None:
        verified = ViewerInspection("verified", (self.viewer,), True)
        with (
            patch("rofi_tmux_plus.viewer_service.os.pidfd_open", return_value=90) as open_pidfd,
            patch("rofi_tmux_plus.viewer_service.os.close"),
            patch("rofi_tmux_plus.viewer_service.signal.pidfd_send_signal") as send_signal,
            patch("rofi_tmux_plus.viewer_service._proc", side_effect=self.processes.get),
            patch(
                "rofi_tmux_plus.viewer_service._niri_windows",
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
        with patch("rofi_tmux_plus.viewer_service.os.pidfd_open") as open_pidfd:
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
        with patch("rofi_tmux_plus.viewer_service.os.pidfd_open") as open_pidfd:
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
                "rofi_tmux_plus.viewer_service.os.pidfd_open",
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
            patch("rofi_tmux_plus.lifecycle.local_mutation_lock", return_value=nullcontext()),
            patch(
                "rofi_tmux_plus.lifecycle.inspect_viewers",
                return_value=ViewerInspection("verified", (viewer,), True),
            ),
            patch("rofi_tmux_plus.lifecycle.focus_window", return_value=True) as focus,
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
            patch("rofi_tmux_plus.lifecycle.local_mutation_lock", return_value=nullcontext()),
            patch(
                "rofi_tmux_plus.lifecycle.launch_metadata",
                return_value=("expected-launch", {"ROFI_TMUX_PLUS_VIEWER_V1": "{}"}),
            ),
            patch("rofi_tmux_plus.lifecycle.inspect_viewers", side_effect=states),
            patch("rofi_tmux_plus.lifecycle.time.sleep"),
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
                patch("rofi_tmux_plus.lifecycle.local_mutation_lock", return_value=nullcontext()),
                patch(
                    "rofi_tmux_plus.lifecycle.inspect_viewers",
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
            patch("rofi_tmux_plus.lifecycle.local_mutation_lock", return_value=nullcontext()),
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
            patch("rofi_tmux_plus.lifecycle.local_mutation_lock", return_value=nullcontext()),
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
