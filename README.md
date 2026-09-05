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
  -kb-custom-6 Escape \
  -kb-cancel Control+g \
  -kb-move-char-forward Control+f -kb-move-char-back Control+b \
  -eh 2
```

Rofi must invoke the executable as a script mode and provide the callbacks
above. `Alt+R` is a bounded foreground refresh; Right and Left wrap the
`Recent` and `Hosts` roots; Enter drills into a host or opens a session;
`Ctrl+Enter` creates/opens a named session or commits a rename; F2 begins a
rename; Shift+Delete asks for kill confirmation; Escape backs out of a host
layer or pending action and exits at a root; and `Ctrl+G` always exits. Tab
and Shift+Tab remain Rofi's normal row navigation. `Ctrl+B` and `Ctrl+F` move
the filter cursor. `-eh 2` reserves the two physical Pango display lines used
by each session row.

The callback boundary fails closed: configuration, model, and callback errors
are rendered as bounded notices. Root Escape returns no rows before setup, and
nested Escape recovers to the enclosing browsing root if a model snapshot
cannot be loaded. Pending actions are cleared on that recovery, while Ctrl+G
stays Rofi's native unconditional cancel binding.

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
