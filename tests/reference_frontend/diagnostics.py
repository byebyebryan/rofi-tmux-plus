"""Shared pure/support type identity for the frozen test baseline."""

import sys

from rofi_tmux_plus import diagnostics

sys.modules[__name__] = diagnostics
