"""Quiet notifications, conservative watch loss and owned child lifetime."""

import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.native_mode import verify_native
from rofi_tmux_plus.observer_client import observer_api
from rofi_tmux_plus.picker_notify import (
    decode_notification,
    read_notification,
    write_notification,
)
from rofi_tmux_plus.picker_watch import WATCH_ENTRY, OwnedWatch, notification_material
from rofi_tmux_plus.prepared_model import apply_expiry, boottime_ms, clock_domain, project_frame


def frame_fixture():
    frame = json.loads((Path(__file__).parent / "fixtures/prepared-fleet-frame.json").read_text())
    shift = boottime_ms() - 130
    clock = clock_domain()

    def translate(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "clock":
                    value[key] = clock.copy()
                elif (
                    key
                    in {
                        "encodedAt",
                        "startedAt",
                        "finishedAt",
                        "acceptedAt",
                        "expiresAt",
                        "lastAttemptAt",
                        "localExpiry",
                    }
                    and type(item) is int
                ):
                    value[key] += shift
                else:
                    translate(item)
        elif isinstance(value, list):
            for item in value:
                translate(item)

    translate(frame)
    observer_api().protocol.validate_fleet_frame(frame)
    return frame


class NotificationTests(unittest.TestCase):
    def test_metadata_contains_only_bounded_wakeup_numbers(self):
        value = decode_notification(b"TP1 1 1000 9000 42 1\n")
        self.assertTrue(value["ready"])
        for raw in (
            b"TP1 1 1000 9000 0 1\n",
            b"TP1 -1 1000 9000 42 1\n",
            b"TP1 1 1000 9000 42 01\n",
            b"TP1 1 1000 9000 42 1\nrm -rf /\n",
            b"TP1 1 1000 9000 42 1\x00\n",
            b"TP1 1 9223372036854775808 9000 42 1\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_notification(raw)

    def test_quiet_lease_renewal_does_not_wake_but_ticket_and_material_changes_do(self):
        frame = frame_fixture()
        now = boottime_ms()
        first, expiry = notification_material(frame, now)
        changed = copy.deepcopy(frame)
        changed["snapshot"]["hosts"][0]["owner"]["receipt"]["acceptedAttempt"] += 1
        changed["snapshot"]["hosts"][0]["owner"]["localExpiry"] += 1000
        changed["snapshot"]["viewRevision"] += 1
        renewed, renewed_expiry = notification_material(changed, now)
        self.assertEqual(first, renewed)
        self.assertEqual(renewed_expiry, expiry + 1000)
        changed["ticket"] = {"id": "owned", "state": "complete"}
        self.assertNotEqual(first, notification_material(changed, now)[0])
        changed["ticket"] = None
        changed["snapshot"]["hosts"][0]["sessions"][0]["name"] = "renamed"
        self.assertNotEqual(first, notification_material(changed, now)[0])
        self.assertNotEqual(first, notification_material(frame, expiry)[0])
        changed = copy.deepcopy(frame)
        changed["kind"] = "resync"
        self.assertNotEqual(first, notification_material(changed, now)[0])

    def test_watch_loss_revokes_cached_positives_without_waiting_for_owner_expiry(self):
        frame = frame_fixture()
        now = boottime_ms()
        with tempfile.TemporaryDirectory() as temporary:
            scope = frame["clock"], frame["contextId"]
            payload = project_frame(frame, now=now, scope=scope)
            payload["prepared"]["watchRuntime"] = temporary
            write_notification(temporary, sequence=1, received=now, pid=os.getpid(), ready=True)
            current = apply_expiry(payload, now=now, scope=scope)
            self.assertEqual(current["hosts"][0]["status"], "ok")
            write_notification(temporary, sequence=2, received=now, ready=False)
            lost = apply_expiry(current, now=now, scope=scope)
            self.assertEqual(lost["hosts"][0]["status"], "unavailable")
            self.assertEqual(lost["hosts"][0]["error"]["code"], "watch_unavailable")
            self.assertFalse(lost["viewerFactsCurrent"])
            self.assertGreater(payload["hosts"][0]["ownerExpiry"], now)


class OwnedWatchTests(unittest.TestCase):
    def test_parent_guard_refuses_a_replaced_launcher_before_importing_the_client(self):
        result = subprocess.run(
            [sys.executable, "-I", "-c", WATCH_ENTRY, "0", "watch", "--json"],
            capture_output=True,
            check=False,
            timeout=3,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_launcher_crash_terminates_and_reaps_its_guarded_watch(self):
        # The isolated supervisor adopts its own grandchildren, so this test
        # leaves neither an orphan watch nor an unreaped process behind.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "tmux_observer_client"
            package.mkdir()
            (package / "__init__.py").touch()
            (package / "cli.py").write_text(
                "import time\ndef main():\n print('ready',flush=True)\n time.sleep(30)\n"
            )
            parent_script = (
                "import json,os,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',json.loads(sys.argv[1]),"
                "str(os.getpid())],stdout=subprocess.PIPE,text=True); "
                "assert child.stdout.readline().strip()=='ready'; "
                "print(child.pid,flush=True); sys.stdin.read(); child.wait()"
            )
            supervisor = """
import ctypes,json,os,signal,subprocess,sys,time
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(36,1,0,0,0)==0
parent=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
child=None
try:
    child=int(parent.stdout.readline())
    parent.kill()
    parent.wait(timeout=2)
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        pid,status=os.waitpid(child,os.WNOHANG)
        if pid:
            print(json.dumps({'signal':os.waitstatus_to_exitcode(status)}))
            child=None
            break
        time.sleep(.01)
    assert child is None,'guarded watch survived its parent'
finally:
    if parent.poll() is None:
        parent.kill()
        parent.wait()
    if child is not None:
        os.kill(child,signal.SIGKILL)
        os.waitpid(child,0)
"""
            result = subprocess.run(
                [sys.executable, "-c", supervisor, parent_script, json.dumps(WATCH_ENTRY)],
                env=dict(os.environ, PYTHONPATH=temporary),
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"signal": -15})

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("owned watch did not reach the expected bounded state")

    def child_command(self, root, frame, *, suffix="time.sleep(10)"):
        fixture = Path(root) / "input.json"
        fixture.write_text(json.dumps(frame))
        script = (
            "import sys,json,time; "
            "v=json.load(open(sys.argv[1])); "
            "print(json.dumps(v),flush=True); " + suffix
        )
        return [sys.executable, "-c", script, str(fixture)]

    def test_real_pipe_child_is_one_owned_subscription_and_cancel_reaps_it(self):
        frame = frame_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            watch = OwnedWatch(
                temporary,
                frame["contextId"],
                os.environ,
                command=self.child_command(temporary, frame),
            )
            with watch:
                self.wait_for(lambda: read_notification(temporary)["ready"])
                child = watch.child
                self.assertEqual(read_notification(temporary)["pid"], child.pid)
                with self.assertRaises(ValueError):
                    watch.start()
                self.assertIs(watch.child, child)
            self.assertFalse(watch.thread.is_alive())
            self.assertIsNotNone(child.poll())
            self.assertIsNone(watch.child)
            self.assertFalse(read_notification(temporary)["ready"])

    def test_replayed_or_foreign_frame_ends_subscription_and_fences_the_notification(self):
        for kind in ("replayed", "foreign", "malformed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                frame = frame_fixture()
                suffix = "print(json.dumps(v),flush=True); time.sleep(10)"
                if kind == "foreign":
                    suffix = (
                        "v['contextId']='f'*32; print(json.dumps(v),flush=True); time.sleep(10)"
                    )
                elif kind == "malformed":
                    suffix = "print('broken',flush=True); time.sleep(10)"
                watch = OwnedWatch(
                    temporary,
                    frame["contextId"],
                    os.environ,
                    command=self.child_command(temporary, frame, suffix=suffix),
                )
                with watch:
                    self.wait_for(
                        lambda watch=watch, temporary=temporary: (
                            watch.sequence >= 2 and not read_notification(temporary)["ready"]
                        )
                    )
                self.assertIsNone(watch.child)
                self.assertFalse(watch.thread.is_alive())

    def test_removed_runtime_ends_watch_without_creating_a_replacement_directory(self):
        frame = frame_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "owned"
            root.mkdir(mode=0o700)
            watch = OwnedWatch(
                root,
                frame["contextId"],
                os.environ,
                command=self.child_command(
                    root,
                    frame,
                    suffix="time.sleep(0.2); print(json.dumps(v),flush=True); time.sleep(10)",
                ),
            )
            with watch:
                self.wait_for(lambda: read_notification(root)["ready"])
                for path in root.iterdir():
                    path.unlink()
                root.rmdir()
                self.wait_for(lambda: not watch.thread.is_alive())
            self.assertFalse(root.exists())
            self.assertIsNone(watch.child)

    def test_simulated_callback_pause_expires_silent_pipe_and_reaps_owned_child(self):
        frame = frame_fixture()
        clock = [boottime_ms()]
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("rofi_tmux_plus.picker_watch.boottime_ms", side_effect=lambda: clock[0]),
        ):
            watch = OwnedWatch(
                temporary,
                frame["contextId"],
                os.environ,
                command=self.child_command(temporary, frame),
            )
            with watch:
                self.wait_for(lambda: read_notification(temporary)["ready"])
                child = watch.child
                clock[0] += 30000
                self.wait_for(lambda: not read_notification(temporary)["ready"])
                self.assertIsNotNone(child.poll())
            self.assertIsNone(watch.child)
            self.assertFalse(watch.thread.is_alive())


class NativeTupleTests(unittest.TestCase):
    def test_drift_or_unsupported_binary_refuses_before_any_native_mode_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "rofi"
            binary.write_bytes(b"fixture binary")
            files = {}
            for name in ("tmux-plus.so", "tmux-plus-notify.c", "rofi-tmux-plus-mode"):
                path = root / name
                path.write_bytes(name.encode())
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            (root / "rofi-tmux-plus-mode").chmod(0o755)
            descriptor = {
                "schemaVersion": 1,
                "modeAbi": 7,
                "architecture": platform.machine(),
                "rofiSha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "files": files,
            }
            (root / "native-artifact.json").write_text(json.dumps(descriptor))
            with patch("subprocess.Popen", side_effect=AssertionError("native mode loaded")):
                self.assertEqual(
                    verify_native(directory=root, executable=binary), (str(binary), root)
                )
                binary.write_bytes(b"changed binary")
                with self.assertRaisesRegex(ContractError, "binary differs"):
                    verify_native(directory=root, executable=binary)
                binary.write_bytes(b"fixture binary")
                (root / "tmux-plus.so").write_bytes(b"changed mode")
                with self.assertRaisesRegex(ContractError, "bytes differ"):
                    verify_native(directory=root, executable=binary)
