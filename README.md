# rofi-tmux-plus

`rofi-tmux-plus` is a proposed Rofi picker and manager for local and remote
tmux sessions. It will present one mixed session inventory, use
`rofi-ssh-plus` for logical hosts and SSH routes, and expose generic tmux
lifecycle operations for `rofi-agent-plus`.

This repository is currently design-only. No picker, command-line interface,
package, or release has been implemented.

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
