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

The private retained-remote cache and bounded detached refresh owner support a
Rofi browse surface with Open and guarded Kill actions. The public lifecycle
contract continues to expose `create` and `rename` for CLI consumers.

For development without installing the console script:

```sh
PYTHONPATH=. ./bin/rofi-tmux-plus inventory --json
rofi -show tmux-plus -modes "tmux-plus:$(pwd)/bin/rofi-tmux-plus" \
  -kb-custom-1 Alt+r -kb-custom-2 Right -kb-custom-3 Left \
  -kb-custom-7 Tab -kb-custom-8 ISO_Left_Tab \
  -kb-element-next "" -kb-element-prev "" \
  -kb-accept-custom "" -kb-delete-entry "" \
  -kb-cancel Escape,Control+g \
  -kb-move-char-forward Control+f -kb-move-char-back Control+b \
  -eh 2
```

Rofi must invoke the executable as a script mode and provide the callbacks
above. Open is the initial action; Tab advances to Kill and Shift+Tab reverses
the ordered action cycle, with wraparound. The prompt shows the host scope;
the persistent message shows the active Enter action, next Tab action, and any
notice. Enter opens or begins a kill confirmation for the session
highlighted at that moment. It never acts on Tab. Right and Left wrap the flat
`All`, `Local`, and Host Mesh remote scopes, while `Alt+R` performs a bounded
foreground refresh. Escape and `Ctrl+G` use Rofi's native cancel action and
always close the picker. Up and Down move rows; `Ctrl+B` and `Ctrl+F` move the
filter cursor. `-eh 2` reserves the two physical Pango display lines used by
each session row.

The callback boundary fails closed: configuration, model, and callback errors
are rendered as bounded notices. Unknown action state blocks Enter visibly.
Legacy callback numbers 2, 3, 13, and 15 are immediate no-ops for stale open
windows, so retired custom create, direct delete, F2 rename, and callback
Escape paths cannot mutate a session. Current Escape and `Ctrl+G` never enter
the script callback path. Pending kill confirmation is separate from the
browse action and native cancel discards it.

Each model render is retained in a private content-addressed snapshot cache so
Left/Right and Tab/Shift+Tab callbacks can render from the exact presentation
without reading Host Mesh or local tmux. The cache keeps the newest 256 owned
snapshots and fails closed if the exact snapshot in `ROFI_DATA` is missing or
corrupt. Refreshes retain the active action and use stable typed row identity
to select the same session after a reorder when it still exists.

The picker also uses Rofi's row-state tokens to separate observation confidence
from session age. A live `open here` session row is `active`; retained session
rows whose host observation is unavailable are `urgent` but remain selectable.
Live attached and detached rows, background refreshes, and old activity times
remain visually ordinary in Open mode. In Kill mode every selectable session
row combines `active` and `urgent`, allowing the managed selected-row theme to
show the destructive action. A normal empty-scope row is only
`nonselectable`, while an empty concrete scope whose host is unavailable is
also `urgent`. The confirmation row also combines `active` and `urgent` and
names the host, session, and attached-client impact. A successful atomic
refresh naturally removes an unavailable marker on the next render; cache age
alone never creates one.

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
