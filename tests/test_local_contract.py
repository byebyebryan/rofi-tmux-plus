from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from rofi_tmux_plus import cli
from rofi_tmux_plus.config import Config, load_config
from rofi_tmux_plus.errors import ContractError, NoServer
from rofi_tmux_plus.host import local_host
from rofi_tmux_plus.lifecycle import LocalLifecycle, _wrapper_command
from rofi_tmux_plus.model import Session, SessionReference
from rofi_tmux_plus.tmux import TmuxClient


class IsolatedServer(unittest.TestCase):
    """Every destructive test targets one disposable, non-default server."""

    def setUp(self) -> None:
        self._original_shell = os.environ.get("SHELL")
        os.environ["SHELL"] = "/bin/sh"
        self.socket = f"rofi-tmux-plus-test-{secrets.token_hex(8)}"
        self.argv = ("tmux", "-L", self.socket, "-f", "/dev/null")
        self.client = TmuxClient(self.argv, timeout_seconds=2)
        self.host = local_host("Local.Example")
        self.lifecycle = LocalLifecycle(
            self.client,
            Config(terminal=("true",)),
            host=self.host,
            niri_command=("definitely-not-niri",),
            terminal_spawner=lambda _session_id: None,
        )

    def tearDown(self) -> None:
        # Exact generated socket only; this never addresses the user's default
        # server.  The executable prefix is asserted in the test below too.
        subprocess.run(
            [*self.argv, "kill-server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if self._original_shell is None:
            os.environ.pop("SHELL", None)
        else:
            os.environ["SHELL"] = self._original_shell

    def create_direct(self, name: str = "alpha") -> tuple[str, int]:
        return self.client.create_detached(name, "/tmp", ["/bin/sh", "-c", "sleep 30"])

    def test_no_server_is_authoritative_empty_and_never_default(self) -> None:
        self.assertIn("-L", self.client._executable)
        generation, sessions = self.client.inventory(self.host.host_id)
        self.assertIsNone(generation)
        self.assertEqual(sessions, [])

    def test_inventory_includes_stable_identity_panes_options_and_pending(self) -> None:
        self.create_direct()
        self.client.run(["new-window", "-d", "-t", "$0", "/bin/sh", "-c", "sleep 30"])
        self.client.set_option("$0", "@present", "provider-id")
        self.client.set_option("$0", "@rofi_tmux_plus_pending", "token")
        generation, sessions = self.client.inventory(
            self.host.host_id, panes=True, option_names=["@present", "@absent"]
        )
        self.assertIsNotNone(generation)
        self.assertEqual(len(sessions), 1)
        row = sessions[0].as_dict()
        self.assertEqual(row["sessionId"], "$0")
        self.assertTrue(row["pending"])
        self.assertEqual(row["options"], {"@present": "provider-id", "@absent": None})
        self.assertEqual(len(row["panes"]), 2)

    def test_running_empty_server_has_generation_and_authoritative_empty_sessions(self) -> None:
        session_id, _created_at = self.create_direct()
        self.client.run(["set-option", "-g", "exit-empty", "off"])
        self.client.kill(session_id)
        generation, sessions = self.client.inventory(self.host.host_id)
        self.assertIsNotNone(generation)
        self.assertEqual(sessions, [])

    def test_open_accepts_external_rename_but_mutations_require_observed_name(self) -> None:
        self.create_direct("before")
        _generation, sessions = self.client.inventory(self.host.host_id)
        reference = sessions[0].reference
        self.client.rename(reference.session_id, "after")
        opened = self.lifecycle.open(
            self.host.host_id,
            None,
            reference.server_generation,
            reference.session_id,
            reference.created_at,
        )
        self.assertTrue(opened["terminalLaunched"])
        with self.assertRaisesRegex(ContractError, "selected tmux session changed"):
            self.lifecycle.rename(
                self.host.host_id,
                None,
                reference.server_generation,
                reference.session_id,
                reference.created_at,
                "before",
                "new",
            )

    def test_restart_with_reused_id_is_stale(self) -> None:
        self.create_direct("first")
        _generation, sessions = self.client.inventory(self.host.host_id)
        old = sessions[0].reference
        self.client.try_run(["kill-server"])
        time.sleep(0.1)
        self.create_direct("second")
        with self.assertRaisesRegex(ContractError, "tmux server changed"):
            self.lifecycle.open(
                self.host.host_id, None, old.server_generation, old.session_id, old.created_at
            )

    def test_create_default_and_collision(self) -> None:
        made = self.lifecycle.create(
            self.host.host_id, None, "managed", "/tmp", [("@provider", "x")], [], False, None, False
        )
        self.assertFalse(made["session"]["pending"])
        self.assertNotIn("options", made["session"])
        self.assertEqual(self.client.option(made["session"]["sessionId"], "@provider"), "x")
        with self.assertRaisesRegex(ContractError, "exact name already exists"):
            self.lifecycle.create(
                self.host.host_id, None, "managed", "/tmp", [], [], False, None, False
            )

    def test_invalid_cwd_never_falls_back(self) -> None:
        with self.assertRaisesRegex(ContractError, "cwd must name an existing directory"):
            self.lifecycle.create(
                self.host.host_id,
                None,
                "managed",
                "/definitely/not/a/directory",
                [],
                [],
                False,
                None,
                False,
            )

    def test_first_post_token_metadata_failure_rolls_back_exact_session(self) -> None:
        original = self.client.set_option

        def fail_after_guard(session_id: str, name: str, value: str) -> None:
            if name == "@fail":
                raise ContractError("operation_failed", "synthetic option failure")
            original(session_id, name, value)

        self.client.set_option = fail_after_guard  # type: ignore[method-assign]
        with self.assertRaisesRegex(ContractError, "synthetic option failure"):
            self.lifecycle.create(
                self.host.host_id,
                None,
                "will-rollback",
                "/tmp",
                [("@fail", "x")],
                [],
                False,
                None,
                False,
            )
        with self.assertRaises(NoServer):
            self.client.session_ids()

    def test_early_holder_disappearance_returns_stable_operation_failure(self) -> None:
        """A holder that disappears before server identity is read has a stable error."""
        original_path = os.environ.get("PATH", "")
        with tempfile.TemporaryDirectory() as directory:
            fake_tmux = Path(directory) / "tmux"
            fake_tmux.write_text(
                "#!/bin/sh\n"
                "new_session=0\n"
                "socket=\n"
                "previous=\n"
                'for value in "$@"; do\n'
                '  [ "$value" = "new-session" ] && new_session=1\n'
                '  [ "$previous" = "-L" ] && socket="$value"\n'
                '  previous="$value"\n'
                "done\n"
                'if [ "$new_session" = 1 ]; then\n'
                '  output=$(/usr/bin/tmux "$@") || exit $?\n'
                '  printf "%s\\n" "$output"\n'
                '  session_id=$(printf "%s\\n" "$output" | awk \'{print $1}\')\n'
                '  while /usr/bin/tmux -L "$socket" -f /dev/null has-session -t "$session_id" 2>/dev/null; do\n'
                "    sleep 0.01\n"
                "  done\n"
                "  exit 0\n"
                "fi\n"
                'for value in "$@"; do\n'
                '  [ "$value" = "@rofi_tmux_plus_operation" ] && exit 1\n'
                "done\n"
                'exec /usr/bin/tmux "$@"\n',
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            os.environ["PATH"] = f"{directory}:{original_path}"
            try:
                with self.assertRaises(ContractError) as raised:
                    self.lifecycle.create(
                        self.host.host_id,
                        None,
                        "token-install-failure",
                        "/tmp",
                        [],
                        [],
                        False,
                        None,
                        False,
                    )
                self.assertEqual(raised.exception.code, "operation_failed")
                self.assertEqual(
                    raised.exception.message,
                    "holding wrapper did not install its operation token",
                )
            finally:
                os.environ["PATH"] = original_path
        with self.assertRaises(NoServer):
            self.client.session_ids()

    def test_token_or_reference_mismatch_is_never_killed_by_rollback(self) -> None:
        original = self.client.set_option

        def replace_token_then_fail(session_id: str, name: str, value: str) -> None:
            if name == "@fail":
                original(session_id, "@rofi_tmux_plus_operation", "someone-else")
                raise ContractError("operation_failed", "synthetic option failure")
            original(session_id, name, value)

        self.client.set_option = replace_token_then_fail  # type: ignore[method-assign]
        with self.assertRaisesRegex(ContractError, "synthetic option failure"):
            self.lifecycle.create(
                self.host.host_id,
                None,
                "must-survive",
                "/tmp",
                [("@fail", "x")],
                [],
                False,
                None,
                False,
            )
        generation, sessions = self.client.inventory(self.host.host_id)
        self.assertIsNotNone(generation)
        self.assertEqual([session.name for session in sessions], ["must-survive"])

    def test_bookkeeping_cleanup_failure_after_release_keeps_success(self) -> None:
        def fail_unset(_session_id: str, _name: str) -> None:
            raise ContractError("operation_failed", "synthetic cleanup failure")

        self.client.unset_option = fail_unset  # type: ignore[method-assign]
        made = self.lifecycle.create(
            self.host.host_id, None, "released", "/tmp", [], [], False, None, False
        )
        self.assertTrue(made["ok"])
        self.assertIn(made["session"]["sessionId"], self.client.session_ids())

    def test_deferred_session_times_out_and_removes_its_own_pending_session(self) -> None:
        made = self.lifecycle.create(
            self.host.host_id,
            None,
            "deferred",
            "/tmp",
            [],
            ["/bin/sh", "-c", "sleep 30"],
            True,
            1,
            False,
        )
        self.assertTrue(made["session"]["pending"])
        time.sleep(2.1)
        with self.assertRaises(NoServer):
            self.client.session_ids()


class UnitContractTests(unittest.TestCase):
    def test_host_aliases_are_casefolded_safe(self) -> None:
        host = local_host("Desk.TOP.Example")
        self.assertEqual(host.host_id, "desk")
        self.assertIn("Desk.TOP.Example", host.aliases)

    def test_host_alias_parity_preserves_underscore_and_rejects_unsafe_tokens(self) -> None:
        underscored = local_host("Desk_1.Example")
        self.assertEqual(underscored.host_id, "desk_1")
        self.assertEqual(underscored.aliases, frozenset({"Desk_1", "Desk_1.Example"}))
        option_like = local_host("-bad.example")
        self.assertEqual(option_like.host_id, "localhost")
        self.assertEqual(option_like.aliases, frozenset({"localhost"}))
        whitespace = local_host("bad host.example")
        self.assertEqual(whitespace.host_id, "localhost")
        self.assertEqual(whitespace.aliases, frozenset({"localhost"}))
        control = local_host("bad\u200ehidden.example")
        self.assertEqual(control.host_id, "localhost")
        self.assertNotIn("\u200e", control.native_hostname)

    def test_wrapper_keeps_user_argv_out_of_shell_source(self) -> None:
        argv = _wrapper_command(
            "token", ["program", "$(not-a-shell-expansion)"], defer=False, timeout=60
        )
        self.assertNotIn("$(not-a-shell-expansion)", argv[2])
        self.assertEqual(argv[-2:], ["program", "$(not-a-shell-expansion)"])

    def test_strict_config_rejects_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "config.toml"
            temporary.write_text("schema_version = 1\nextra = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unknown configuration key"):
                load_config(temporary)

    def test_partial_v1_config_uses_defaults_and_rejects_unicode_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "config.toml"
            temporary.write_text("schema_version = 1\n", encoding="utf-8")
            self.assertEqual(load_config(temporary), Config())
            temporary.write_text("schema_version = 1\nrefresh_seconds = 9\n", encoding="utf-8")
            self.assertEqual(load_config(temporary), Config(refresh_seconds=9))
            temporary.write_text(
                'schema_version = 1\nterminal = ["ghostty\u200e"]\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractError, "terminal"):
                load_config(temporary)

    def test_fixture_scenarios_have_contract_shapes(self) -> None:
        root = Path(__file__).parents[1] / "contracts" / "tmux-session-v1" / "fixtures"
        validators = {
            "no-server.json": lambda value: (
                value["response"]["hosts"][0]["serverGeneration"] is None
            ),
            "running-empty.json": lambda value: value["response"]["hosts"][0]["sessions"] == [],
            "multiple-sessions.json": lambda value: (
                len(value["response"]["hosts"][0]["sessions"]) == 2
            ),
            "panes-options-pending.json": lambda value: value["response"]["hosts"][0]["sessions"][
                0
            ]["pending"],
            "reused-session-id.json": lambda value: (
                value["response"]["error"]["code"] == "stale_session"
            ),
            "external-rename.json": lambda value: (
                value["rename"]["error"]["code"] == "stale_session"
            ),
            "create-default.json": lambda value: not value["response"]["session"]["pending"],
            "create-deferred.json": lambda value: value["response"]["session"]["pending"],
            "session-exists.json": lambda value: (
                value["response"]["error"]["code"] == "session_exists"
            ),
            "invalid-cwd.json": lambda value: value["response"]["error"]["code"] == "invalid_cwd",
            "remote-lifecycle.json": lambda value: (
                value["open"]["meshRevision"] == value["meshRevision"]
                and value["create"]["session"]["hostId"] == "beta"
                and value["errors"]["stale_mesh"]["error"]["code"] == "stale_mesh"
            ),
            "setup-rollback.json": lambda value: value["assertions"]["requiresOperationToken"],
            "deferred-timeout.json": lambda value: value["assertions"]["requiresPendingToken"],
            "envelopes.json": lambda value: (
                set(value["errors"])
                == {
                    "unknown_host",
                    "stale_mesh",
                    "host_unreachable",
                    "tmux_missing",
                    "session_not_found",
                    "session_exists",
                    "stale_session",
                    "invalid_input",
                    "invalid_cwd",
                    "launch_failed",
                    "operation_failed",
                }
            ),
        }
        for name, validate in validators.items():
            fixture = json.loads((root / name).read_text(encoding="utf-8"))
            self.assertTrue(validate(fixture), name)

    def test_fixture_set_is_complete_and_json(self) -> None:
        root = Path(__file__).parents[1] / "contracts" / "tmux-session-v1" / "fixtures"
        expected = {
            "no-server.json",
            "running-empty.json",
            "multiple-sessions.json",
            "panes-options-pending.json",
            "reused-session-id.json",
            "external-rename.json",
            "create-default.json",
            "create-deferred.json",
            "session-exists.json",
            "invalid-cwd.json",
            "remote-lifecycle.json",
            "setup-rollback.json",
            "deferred-timeout.json",
            "envelopes.json",
        }
        self.assertEqual({path.name for path in root.glob("*.json")}, expected)
        for path in root.glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))


class FocusAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = local_host("Local.Example")
        self.session = Session(
            SessionReference(self.host.host_id, "tmux-v1:1:2:/socket", "$0", 1),
            "agent",
            1,
            None,
            0,
            False,
            1,
            "/tmp",
            "shell",
            "/tmp",
        )
        self.lifecycle = LocalLifecycle(
            TmuxClient(("definitely-not-tmux",)),
            Config(terminal=("ghostty",)),
            host=self.host,
        )

    def test_focus_matches_exact_session_prefix_and_short_host_suffix(self) -> None:
        windows = json.dumps([{"id": 42, "title": "agent:0 workspace @ LOCAL"}])
        with (
            patch.dict(os.environ, {"NIRI_SOCKET": "/tmp/niri-test"}),
            patch("rofi_tmux_plus.lifecycle.shutil.which", return_value="/usr/bin/niri"),
            patch(
                "rofi_tmux_plus.lifecycle.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["niri"], 0, windows, ""),
                    subprocess.CompletedProcess(["niri"], 0, "", ""),
                ],
            ) as run,
        ):
            self.assertTrue(self.lifecycle._focus_matching_window(self.session))
        self.assertIn("focus-window", run.call_args_list[1].args[0])

    def test_focus_rejects_other_host_and_terminal_uses_scope_then_fallback(self) -> None:
        other_host = json.dumps([{"id": 42, "title": "agent:0 workspace @ OTHER"}])
        with (
            patch.dict(os.environ, {"NIRI_SOCKET": "/tmp/niri-test"}),
            patch("rofi_tmux_plus.lifecycle.shutil.which", return_value="/usr/bin/niri"),
            patch(
                "rofi_tmux_plus.lifecycle.subprocess.run",
                return_value=subprocess.CompletedProcess(["niri"], 0, other_host, ""),
            ) as run,
        ):
            self.assertFalse(self.lifecycle._focus_matching_window(self.session))
        self.assertEqual(run.call_count, 1)
        with (
            patch("rofi_tmux_plus.lifecycle.shutil.which", return_value="/usr/bin/systemd-run"),
            patch("rofi_tmux_plus.lifecycle.subprocess.Popen") as spawn,
        ):
            self.lifecycle._spawn_terminal("$0")
        self.assertEqual(
            spawn.call_args.args[0],
            [
                "/usr/bin/systemd-run",
                "--user",
                "--scope",
                "--collect",
                "--quiet",
                "--",
                "ghostty",
                "-e",
                "tmux",
                "attach-session",
                "-t",
                "$0",
            ],
        )
        with (
            patch("rofi_tmux_plus.lifecycle.shutil.which", return_value=None),
            patch("rofi_tmux_plus.lifecycle.subprocess.Popen") as spawn,
        ):
            self.lifecycle._spawn_terminal("$1")
        self.assertEqual(
            spawn.call_args.args[0], ["ghostty", "-e", "tmux", "attach-session", "-t", "$1"]
        )

    def test_invalid_stable_reference_is_rejected_before_tmux(self) -> None:
        for generation, session_id, created_at, expected_name in (
            ("generation", "not-a-session", 1, None),
            ("generation", "$0", -1, None),
            ("bad\u200egeneration", "$0", 1, None),
            ("generation", "$0", 1, "bad\u200ename"),
        ):
            with self.assertRaisesRegex(ContractError, "invalid_input"):
                self.lifecycle.open(
                    self.host.host_id,
                    None,
                    generation,
                    session_id,
                    created_at,
                    expected_name,
                )

    def _main_json(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(argv)
        return status, json.loads(output.getvalue())

    def test_cli_json_errors_and_deduplicated_inventory_dispatch(self) -> None:
        status, error = self._main_json(["create", "--json"])
        self.assertEqual(status, 2)
        self.assertEqual(error["error"]["code"], "invalid_input")

        fake = MagicMock()
        fake.inventory.return_value = {
            "schemaVersion": 1,
            "generatedAt": 1,
            "meshRevision": None,
            "hosts": [
                {
                    "hostId": self.host.host_id,
                    "display": self.host.display,
                    "local": True,
                    "status": "ok",
                    "observedAt": 1,
                    "nativeHostname": self.host.native_hostname,
                    "serverGeneration": None,
                    "route": None,
                    "sessions": [],
                }
            ],
        }
        with patch("rofi_tmux_plus.cli._inventory_service", return_value=fake):
            status, result = self._main_json(
                ["inventory", "--json", "--host", "local", "--host", "local.example"]
            )
        self.assertEqual(status, 0)
        self.assertEqual(len(result["hosts"]), 1)
        self.assertEqual(fake.inventory.call_count, 1)

    def test_cli_open_and_create_dispatch_and_invalid_reference_envelope(self) -> None:
        fake = MagicMock()
        fake.open.return_value = {"schemaVersion": 1, "ok": True}
        fake.create.return_value = {"schemaVersion": 1, "ok": True}
        with patch("rofi_tmux_plus.cli._lifecycle", return_value=fake):
            status, result = self._main_json(
                [
                    "open",
                    "--json",
                    "--host",
                    "local",
                    "--server-generation",
                    "generation",
                    "--session-id",
                    "$0",
                    "--created-at",
                    "1",
                ]
            )
            self.assertEqual(status, 0)
            self.assertTrue(result["ok"])
            status, result = self._main_json(
                ["create", "--json", "--host", "local", "--name", "fresh", "--", "echo", "ok"]
            )
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.create.call_args.args[5], ["echo", "ok"])

        with patch("rofi_tmux_plus.cli._lifecycle", return_value=self.lifecycle):
            status, result = self._main_json(
                [
                    "open",
                    "--json",
                    "--host",
                    "local",
                    "--server-generation",
                    "generation",
                    "--session-id",
                    "bad",
                    "--created-at",
                    "1",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(result["error"]["code"], "invalid_input")
