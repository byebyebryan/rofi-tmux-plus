from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.lifecycle_service import LifecycleService
from rofi_tmux_plus.mesh_adapter import (
    MeshHost,
    MeshPolicy,
    MeshRoute,
    MeshSnapshot,
    MeshStaleError,
)
from rofi_tmux_plus.remote_lifecycle import (
    _HOLDER,
    _REMOTE_PROGRAM,
    RemoteLifecycle,
    _parse_action,
    build_remote_lifecycle_argv,
)


def _done(stdout: str, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fake-ssh"], code, stdout, stderr)


def _field(value: str, *, raw: bool = False) -> str:
    payload = value.encode("utf-8") + (b"" if raw else b"\n")
    return payload.hex()


def _action_output(kind: str, *, name: str = "before", clients: int = 0) -> str:
    lines = [
        "H\t" + _field("beta-native", raw=True),
        "G\t" + "\t".join((_field("/tmp/tmux"), _field("10"), _field("20"))),
        "D\t"
        + "\t".join(
            (
                _field("$0", raw=True),
                _field("11"),
                _field(name),
                _field("12"),
                _field(""),
                _field(str(clients)),
                _field("1"),
                _field("/tmp/work"),
                _field("shell"),
                _field("/tmp/work"),
                _field("0", raw=True),
            )
        ),
    ]
    suffix = f"R\t{kind}"
    if kind == "KILL":
        suffix += f"\t{clients}"
    lines.append(suffix)
    return "\n".join(lines) + "\n"


def _error_output(code: str, message: str) -> str:
    return (
        "H\t"
        + _field("beta-native", raw=True)
        + "\nX\t"
        + _field(code, raw=True)
        + "\t"
        + _field(message, raw=True)
        + "\n"
    )


class _Adapter:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []
        self.stale = False

    def report_route(self, **kwargs: object) -> bool:
        self.reports.append(kwargs)
        if self.stale:
            raise MeshStaleError()
        return True


class _Provider:
    def __init__(self, snapshot: MeshSnapshot | None) -> None:
        self.snapshot = snapshot
        self.loads = 0

    def load(self) -> MeshSnapshot | None:
        self.loads += 1
        return self.snapshot


class _Remote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def open(self, host: MeshHost, *_args: object) -> dict[str, object]:
        self.calls.append(("open", host.host_id))
        return {"schemaVersion": 1, "ok": True, "meshRevision": "sha256:fixture"}


class RemoteLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = MeshPolicy("ssh", 2, 1, 300)
        self.host = MeshHost(
            "beta",
            "Beta",
            False,
            ("beta-native",),
            (MeshRoute("beta-vpn.test", 0, None, None), MeshRoute("beta-lan.test", 1, None, None)),
        )
        self.adapter = _Adapter()
        self.nonce = "0123456789abcdef0123456789abcdef"

    def _lifecycle(
        self, replies: list[subprocess.CompletedProcess[str]], **kwargs: object
    ) -> RemoteLifecycle:
        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return replies.pop(0)

        return RemoteLifecycle(
            self.adapter,
            Config(terminal=("ghostty",)),
            runner=runner,
            nonce_factory=lambda: self.nonce,
            now_millis=lambda: 1234,
            **kwargs,
        )

    def test_open_uses_marked_route_focus_or_detached_ssh_terminal(self) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        launched: list[list[str]] = []
        lifecycle = self._lifecycle(
            [_done(_action_output("OPEN"), marker)],
            focus=lambda _session, native: native == "not-this-host",
            terminal_spawner=lambda argv: launched.append(list(argv)),
        )
        result = lifecycle.open(
            self.host, self.policy, "sha256:fixture", "tmux-v1:10:20:/tmp/tmux", "$0", 11, None
        )
        self.assertTrue(result["terminalLaunched"])
        self.assertFalse(result["focused"])
        self.assertEqual(
            launched,
            [["ssh", "-t", "beta-vpn.test", "tmux attach-session -t '$0'"]],
        )
        self.assertEqual(self.adapter.reports[0]["status"], "reachable")

    def test_launch_executes_a_literal_session_id_through_a_fake_openssh_shell(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            target = directory / "target"
            fake_tmux = directory / "tmux"
            fake_tmux.write_text(f"#!/bin/sh\nprintf '%s' \"$3\" > {target}\n", encoding="utf-8")
            fake_tmux.chmod(0o700)
            fake_ssh = directory / "ssh"
            fake_ssh.write_text(
                '#!/bin/sh\n[ "$1" = -t ] && [ "$2" = beta.test ] || exit 2\nexec sh -c "$3"\n',
                encoding="utf-8",
            )
            fake_ssh.chmod(0o700)
            environment = {**os.environ, "PATH": f"{directory}:{os.environ.get('PATH', '')}"}
            lifecycle = RemoteLifecycle(
                self.adapter,
                Config(),
                terminal_spawner=lambda argv: subprocess.run(argv, check=True, env=environment),
            )
            lifecycle._launch("beta.test", MeshPolicy(str(fake_ssh), 2, 1, 300), "$0")
            self.assertEqual(target.read_text(encoding="utf-8"), "$0")

    def test_unmarked_auth_failure_tries_next_route_without_report(self) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        lifecycle = self._lifecycle(
            [
                _done("", "Permission denied (publickey)\n", 255),
                _done(_action_output("OPEN"), marker),
            ],
            focus=lambda _session, _native: True,
        )
        result = lifecycle.open(
            self.host, self.policy, "sha256:fixture", "tmux-v1:10:20:/tmp/tmux", "$0", 11, None
        )
        self.assertTrue(result["focused"])
        self.assertEqual([report["route"] for report in self.adapter.reports], ["beta-lan.test"])

    def test_marked_error_and_stale_report_are_not_silently_retried(self) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        lifecycle = self._lifecycle([_done(_error_output("stale_session", "changed"), marker)])
        with self.assertRaisesRegex(ContractError, "changed"):
            lifecycle.open(
                self.host,
                self.policy,
                "sha256:fixture",
                "tmux-v1:10:20:/tmp/tmux",
                "$0",
                11,
                None,
            )
        self.adapter.stale = True
        lifecycle = self._lifecycle([_done(_action_output("OPEN"), marker)])
        with self.assertRaises(MeshStaleError):
            lifecycle.open(
                self.host,
                self.policy,
                "sha256:fixture",
                "tmux-v1:10:20:/tmp/tmux",
                "$0",
                11,
                None,
            )

    def test_error_framing_requires_only_a_well_formed_leading_hostname_record(self) -> None:
        with self.assertRaisesRegex(ContractError, "changed"):
            _parse_action(
                _error_output("stale_session", "changed"), host_id="beta", route="beta.test"
            )
        valid_header = "H\t" + _field("beta-native", raw=True)
        malformed = (
            "X\t" + _field("stale_session", raw=True) + "\t" + _field("changed", raw=True),
            "H\tnot-hex\nX\t"
            + _field("stale_session", raw=True)
            + "\t"
            + _field("changed", raw=True),
            valid_header
            + "\nX\t"
            + _field("stale_session", raw=True)
            + "\t"
            + _field("changed", raw=True)
            + "\nnoise",
            valid_header
            + "\nX\t"
            + _field("stale_session", raw=True)
            + "\t"
            + _field("changed", raw=True)
            + "\nX\t"
            + _field("stale_session", raw=True)
            + "\t"
            + _field("again", raw=True),
        )
        for output in malformed:
            with self.subTest(output=output):
                with self.assertRaises(ContractError) as raised:
                    _parse_action(output, host_id="beta", route="beta.test")
                self.assertEqual(raised.exception.code, "operation_failed")

    def test_create_collision_and_kill_envelopes(self) -> None:
        marker = "\x1eROFI_PLUS_REACHED_V1:" + self.nonce + "\x1f\n"
        lifecycle = self._lifecycle(
            [_done(_error_output("session_exists", "already exists"), marker)]
        )
        with self.assertRaisesRegex(ContractError, "already exists"):
            lifecycle.create(
                self.host,
                self.policy,
                "sha256:fixture",
                "taken",
                "/tmp",
                [],
                [],
                False,
                None,
                False,
            )
        lifecycle = self._lifecycle([_done(_action_output("KILL", clients=2), marker)])
        result = lifecycle.kill(
            self.host,
            self.policy,
            "sha256:fixture",
            "tmux-v1:10:20:/tmp/tmux",
            "$0",
            11,
            "before",
        )
        self.assertEqual(result["observedClients"], 2)
        self.assertEqual(result["meshRevision"], "sha256:fixture")

    def test_builder_keeps_hostile_inputs_out_of_fixed_shell_source(self) -> None:
        hostile = "name;$(touch never-run)"
        argv = build_remote_lifecycle_argv(
            "user@beta;$(touch route-never-run)",
            self.policy,
            nonce=self.nonce,
            action="create",
            values=[hostile, "/tmp", "token", "0", "20", "0"],
        )
        self.assertEqual(argv[-2], "user@beta;$(touch route-never-run)")
        self.assertIn("'name;$(touch never-run)'", argv[-1])
        self.assertNotIn(hostile, _REMOTE_PROGRAM)
        self.assertIn("@rofi_tmux_plus_operation", _HOLDER)
        self.assertIn("rollback", _REMOTE_PROGRAM)

    def test_reference_validation_happens_before_ssh(self) -> None:
        calls: list[object] = []
        lifecycle = RemoteLifecycle(
            self.adapter,
            Config(),
            runner=lambda *_args, **_kwargs: calls.append(1),
        )
        with self.assertRaisesRegex(ContractError, "invalid_input"):
            lifecycle.open(
                self.host, self.policy, "sha256:fixture", "bad\u200egeneration", "$0", 1, None
            )
        self.assertEqual(calls, [])

    def test_fixed_remote_program_captures_create_before_release(self) -> None:
        """Run only a fake tmux, never a user/default tmux server."""
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            tmux = directory / "tmux"
            state = directory / "state"
            state.mkdir()
            tmux.write_text(
                "#!/bin/sh\n"
                "command=$1; shift\n"
                f"state={state}\n"
                '[ -f "$state/gone" ] && [ "$command" = display-message ] && exit 1\n'
                "case $command in\n"
                "  display-message)\n"
                '    for value in "$@"; do format=$value; done\n'
                "    case $format in\n"
                "      '#{socket_path}') printf '/tmp/tmux\\n' ;;\n"
                "      '#{start_time}') printf '10\\n' ;;\n"
                "      '#{pid}') printf '20\\n' ;;\n"
                "      '#{session_created}') printf '11\\n' ;;\n"
                "      '#{session_name}') if [ -f \"$state/name\" ]; then cat \"$state/name\"; printf '\\n'; else printf 'before\\n'; fi ;;\n"
                "      '#{session_activity}') printf '12\\n' ;;\n"
                "      '#{session_last_attached}') printf '\\n' ;;\n"
                "      '#{session_attached}') printf '0\\n' ;;\n"
                "      '#{session_windows}') printf '1\\n' ;;\n"
                "      '#{session_path}'|'#{pane_current_path}') printf '/tmp/work\\n' ;;\n"
                "      '#{window_name}') printf 'shell\\n' ;;\n"
                "    esac ;;\n"
                '  show-options) case " $* " in *\' @rofi_tmux_plus_operation\'*) cat "$state/token" ;; esac ;;\n'
                '  set-option) : > "$state/gone" ;;\n'
                "  has-session) exit 1 ;;\n"
                '  rename-session) for value in "$@"; do name=$value; done; printf \'%s\' "$name" > "$state/name" ;;\n'
                "  new-session)\n"
                "    previous=\n"
                '    for value in "$@"; do\n'
                '      [ "$previous" = -s ] && printf \'%s\' "$value" > "$state/name"\n'
                '      [ "$previous" = rofi-tmux-plus-holder ] && printf \'%s\' "$value" > "$state/token"\n'
                "      previous=$value\n"
                "    done\n"
                "    printf '$0 11\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            old_path = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = f"{directory}:{old_path}"
                results = []
                for action, values in (
                    ("open", ["tmux-v1:10:20:/tmp/tmux", "$0", "11", "0", ""]),
                    ("kill", ["tmux-v1:10:20:/tmp/tmux", "$0", "11", "before"]),
                    (
                        "rename",
                        ["tmux-v1:10:20:/tmp/tmux", "$0", "11", "before", "renamed"],
                    ),
                    ("create", ["created", "/tmp", "1", "token-test", "0", "20", "0"]),
                ):
                    results.append(
                        subprocess.run(
                            [
                                "sh",
                                "-c",
                                _REMOTE_PROGRAM,
                                "rofi-tmux-plus-remote",
                                action,
                                *values,
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                    )
            finally:
                os.environ["PATH"] = old_path
            self.assertTrue((state / "gone").exists(), results[-1].stdout + results[-1].stderr)
        for result, kind in zip(results, ("OPEN", "KILL", "RENAME", "CREATE"), strict=True):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                sum(line.startswith("G\t") for line in result.stdout.splitlines()),
                1,
                result.stdout + result.stderr,
            )
            self.assertEqual(sum(line.startswith("D\t") for line in result.stdout.splitlines()), 1)
            self.assertIn(f"R\t{kind}", result.stdout)


class LifecycleServiceTests(unittest.TestCase):
    def test_one_snapshot_revision_gate_and_remote_dispatch(self) -> None:
        local = MeshHost("alpha", "Alpha", True, ("alpha-native",), ())
        remote = MeshHost("beta", "Beta", False, (), (MeshRoute("beta.test", 0, None, None),))
        snapshot = MeshSnapshot(
            "sha256:fixture", "alpha", MeshPolicy("ssh", 2, 1, 300), (local, remote)
        )
        provider = _Provider(snapshot)
        target = _Remote()
        service = LifecycleService(Config(), mesh_adapter=provider, remote_lifecycle=target)  # type: ignore[arg-type]
        result = service.open("beta", "sha256:fixture", "generation", "$0", 1)
        self.assertTrue(result["ok"])
        self.assertEqual(provider.loads, 1)
        self.assertEqual(target.calls, [("open", "beta")])
        with self.assertRaisesRegex(ContractError, "stale_mesh"):
            service.open("beta", "sha256:old", "generation", "$0", 1)
        self.assertEqual(target.calls, [("open", "beta")])

    def test_missing_provider_rejects_mesh_revision_before_local_action(self) -> None:
        provider = _Provider(None)
        service = LifecycleService(Config(), mesh_adapter=provider)
        with self.assertRaisesRegex(ContractError, "stale_mesh"):
            service.open("local", "sha256:old", "generation", "$0", 1)
