# rofi-tmux-plus

`rofi-tmux-plus` is a Rofi picker and manager for local and remote tmux
sessions. It presents one mixed session inventory, uses `rofi-ssh-plus` for
logical hosts and SSH routes, and exposes generic tmux lifecycle operations
for `rofi-agent-plus`.

Tmux Session Contract v1 provides strict versioned JSON inventory across the
local default server and compatible Host Mesh remotes, plus safe `open`,
`create`, `rename`, and `kill` operations on either side. Remote inventory is bounded,
nonce-authenticated, and reports revisioned route health through the public
SSH Plus command. It always targets the local default tmux server; test-only
isolated sockets are not a public CLI feature. It also publishes deterministic
producer fixtures for consumers.

The private retained-remote cache, bounded detached refresh owner, and the
complete Rofi browse/open/create/rename/kill UI are implemented beneath the
public lifecycle contract.

For development without installing the console script:

```sh
PYTHONPATH=. ./bin/rofi-tmux-plus inventory --json
rofi -show tmux-plus -modes "tmux-plus:$(pwd)/bin/rofi-tmux-plus" \
  -kb-custom-1 Alt+r -kb-custom-2 Right -kb-custom-3 Left \
  -kb-custom-4 F2 -kb-delete-entry Shift+Delete \
  -kb-accept-custom Control+Return \
  -kb-cancel Escape,Control+g \
  -kb-move-char-forward Control+f -kb-move-char-back Control+b \
  -eh 2
```

Rofi must invoke the executable as a script mode and provide the callbacks
above. `Alt+R` is a bounded foreground refresh; Right and Left wrap the flat
`All`, `Local`, and Host Mesh remote scopes; Enter opens a session;
`Ctrl+Enter` creates/opens a named session on a concrete host scope or commits
a rename; F2 begins a rename; and Shift+Delete asks for kill confirmation.
Escape and `Ctrl+G` use Rofi's native cancel action and always close the
picker. Tab and Shift+Tab remain Rofi's normal row navigation. `Ctrl+B` and
`Ctrl+F` move the filter cursor. `-eh 2` reserves the two physical Pango
display lines used by each session row.

The callback boundary fails closed: configuration, model, and callback errors
are rendered as bounded notices. Legacy callback number 15 is an immediate
no-op for stale pre-P8 invocations; current Escape and `Ctrl+G` never enter the
script callback path. Pending rename or kill actions are discarded when native
cancel closes the picker.

Each model render is retained in a private content-addressed snapshot cache so
Left/Right callbacks can switch scopes without reading Host Mesh or local tmux.
The cache keeps the newest 256 owned snapshots and fails closed if the exact
snapshot in `ROFI_DATA` is missing or corrupt.

The picker also uses Rofi's row-state tokens to separate observation confidence
from session age. A live `open here` session row is `active`; retained session
rows whose host observation is unavailable are `urgent` but remain selectable.
Live attached and detached rows, background refreshes, and old activity times
remain visually ordinary. A normal empty-scope row is only `nonselectable`,
while an empty concrete scope whose host is unavailable is also `urgent`.
The kill-confirmation row combines `active` and `urgent` so the managed theme
can distinguish destructive danger from an unavailable observation. A
successful atomic refresh naturally removes the unavailable marker on the next
render; cache age alone never creates one.

- [Product and interaction design](docs/DESIGN.md)
- [Tmux Session Contract v1](docs/TMUX_SESSION_V1.md)
- [Host Mesh Contract v1](https://github.com/byebyebryan/rofi-ssh-plus/blob/main/docs/HOST_MESH_V1.md)

The intended suite ownership is:

```text
rofi-ssh-plus ────────> rofi-tmux-plus
  logical hosts          generic tmux lifecycle
       │                         │
       └────────────┬────────────┘
                    v
             rofi-agent-plus
       provider discovery and resume
```

Each layer communicates through versioned JSON commands. It does not import
another repository's Python internals or read another tool's private state.
