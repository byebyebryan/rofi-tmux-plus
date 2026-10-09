"""Shared pure/support type identity for the frozen test baseline."""

import sys

from rofi_tmux_plus import errors

sys.modules[__name__] = errors
