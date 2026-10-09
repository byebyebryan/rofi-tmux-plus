"""Shared pure/support type identity for the frozen test baseline."""

import sys

from rofi_tmux_plus import model

sys.modules[__name__] = model
