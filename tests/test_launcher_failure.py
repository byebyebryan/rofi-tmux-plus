"""Desktop startup failures remain visible without loading a candidate mode."""

import subprocess
import unittest
from unittest.mock import patch

from rofi_tmux_plus import launcher
from rofi_tmux_plus.errors import ContractError


class LauncherFailureTests(unittest.TestCase):
    def test_unsupported_tuple_uses_only_the_bounded_public_error_display(self):
        with (
            patch(
                "rofi_tmux_plus.native_mode.verify_native",
                side_effect=ContractError("unsupported", "binary differs\nrenew acceptance"),
            ),
            patch("rofi_tmux_plus.picker_watch.OwnedWatch") as watch,
            patch("rofi_tmux_plus.launcher.shutil.which", return_value="/usr/bin/rofi"),
            patch("rofi_tmux_plus.launcher.subprocess.run") as display,
        ):
            self.assertEqual(launcher.main([]), 1)
        watch.assert_not_called()
        display.assert_called_once()
        self.assertEqual(
            display.call_args.args[0],
            ["/usr/bin/rofi", "-no-config", "-e", "binary differs renew acceptance"],
        )
        self.assertEqual(display.call_args.kwargs["timeout"], 10)

    def test_failed_or_timed_out_error_display_keeps_the_original_failure_status(self):
        for error in (OSError("display unavailable"), subprocess.TimeoutExpired("rofi", 10)):
            with (
                self.subTest(error=type(error).__name__),
                patch("rofi_tmux_plus.native_mode.verify_native", side_effect=OSError("missing")),
                patch("rofi_tmux_plus.launcher.shutil.which", return_value="/usr/bin/rofi"),
                patch("rofi_tmux_plus.launcher.subprocess.run", side_effect=error),
            ):
                self.assertEqual(launcher.main([]), 1)
