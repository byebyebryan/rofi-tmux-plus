"""Shared pure/support type identity for the frozen test baseline."""

import sys

from rofi_tmux_plus import observer_client

sys.modules[__name__] = observer_client
