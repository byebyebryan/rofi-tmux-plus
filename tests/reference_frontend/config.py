"""Shared pure/support type identity for the frozen test baseline."""

import sys

from rofi_tmux_plus import config

sys.modules[__name__] = config
