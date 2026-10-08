"""Prepare one initial frame and use Rofi's native initial selection option."""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import ContractError, clean_message

INITIAL_FRAME_ENV = "ROFI_TMUX_PLUS_INITIAL_FRAME"
MAX_INITIAL_FRAME_BYTES = 4 * 1024 * 1024


def initial_frame(environ: Mapping[str, str]) -> str | None:
    if environ.get("ROFI_RETV") != "0" or not environ.get(INITIAL_FRAME_ENV):
        return None
    try:
        path = Path(environ[INITIAL_FRAME_ENV])
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_INITIAL_FRAME_BYTES
            ):
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_INITIAL_FRAME_BYTES + 1)
            if len(raw) > MAX_INITIAL_FRAME_BYTES:
                return None
            frame = raw.decode("utf-8")
            # A later mode re-entry must prepare current context. Claim the
            # first frame once without removing an environment-supplied file.
            claimed = os.open(
                str(path) + ".served", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.close(claimed)
            return frame
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError):
        return None


def selected_row(frame: str) -> int:
    prefix = "\x00new-selection\x1f"
    for line in frame.split("\n"):
        if line.startswith(prefix):
            value = line[len(prefix) :]
            if value.isascii() and value.isdecimal() and len(value) <= 8:
                return int(value)
            break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from .native_mode import verify_native
    from .observer_client import observer_api
    from .picker_notify import WATCH_REQUIRED_ENV
    from .picker_watch import OwnedWatch
    from .prepared_model import CONTEXT_ENV, RUNTIME_ENV
    from .rofi import run_rofi

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["-show", "tmux-plus", "-modes", "tmux-plus"]
        for key, value in (
            ("custom-1", "Alt+r"),
            ("custom-2", "Right"),
            ("custom-3", "Left"),
            ("custom-7", "Tab"),
            ("custom-8", "ISO_Left_Tab"),
            ("element-next", ""),
            ("element-prev", ""),
            ("accept-custom", ""),
            ("delete-entry", ""),
            ("cancel", "Escape,Control+g"),
            ("move-char-forward", "Control+f"),
            ("move-char-back", "Control+b"),
        ):
            arguments.extend((f"-kb-{key}", value))
        arguments.extend(("-eh", "2"))
        arguments.extend(("-theme-str", 'entry { placeholder: "Filter sessions"; }'))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ROFI_")}
    output = io.StringIO()
    try:
        executable, native = verify_native()
        # The fixed helper's env-python resolves this selected interpreter.
        environment["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        )
        with TemporaryDirectory(prefix="rofi-tmux-plus-launch-") as temporary:
            environment[RUNTIME_ENV] = temporary
            environment[CONTEXT_ENV] = observer_api().prepared.desktop_context_id(environment)
            environment[WATCH_REQUIRED_ENV] = "1"
            with OwnedWatch(temporary, environment[CONTEXT_ENV], environment):
                with redirect_stdout(output):
                    run_rofi(dict(environment, ROFI_RETV="0"))
                frame = output.getvalue()
                raw = frame.encode("utf-8")
                if len(raw) > MAX_INITIAL_FRAME_BYTES:
                    raise ValueError("initial picker frame is too large")
                path = Path(temporary) / "initial-frame"
                path.write_bytes(raw)
                path.chmod(0o600)
                environment[INITIAL_FRAME_ENV] = str(path)
                return subprocess.run(
                    [
                        executable,
                        "-plugin-path",
                        str(native),
                        "-selected-row",
                        str(selected_row(frame)),
                        "-filter",
                        "",
                        *arguments,
                    ],
                    env=environment,
                    check=False,
                ).returncode
    except (OSError, ValueError, ContractError, RuntimeError) as error:
        message = clean_message(getattr(error, "message", error))
        print(f"rofi-tmux-plus-rofi: {message}", file=sys.stderr)
        binary = shutil.which("rofi")
        if binary is not None:
            # Public error display loads no candidate mode. Desktop bindings
            # otherwise hide stderr when a binary/artifact tuple is unsupported.
            try:
                subprocess.run(
                    [binary, "-no-config", "-e", message],
                    env=environment,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        return 1
    except KeyboardInterrupt:
        return 130
