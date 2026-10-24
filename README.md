# Tmux Plus

A Rofi picker and public CLI for local and remote tmux sessions. Browse prepared
views, focus or attach with Open, and use explicit guarded lifecycle actions.

[Project showcase](https://dev.byebyebryan.com/fleet-plus/projects/tmux-plus/) ·
[Get started](docs/getting-started.md) · [Documentation](docs/README.md)

## Install and start

The published [0.12.0a8 prerelease](https://github.com/byebyebryan/rofi-tmux-plus/releases/tag/v0.12.0a8)
is a native/source bundle paired with the exact Tmux Observer 0.6.0a9 artifact.
Use [getting started](docs/getting-started.md) for prerequisites and the full
launcher invocation. A standalone Plus wheel is not published for this release.

The graphical picker needs the qualified native mode/Rofi tuple and already
running prepared services. Installing Python packages alone is insufficient.
For checkout work, follow [development](docs/maintenance/development.md); arbitrary Observer
source bytes fail the intentional complete-module artifact guard.

## Browse or inspect

| Key | Action |
| --- | --- |
| Up / Down | Select a row |
| Left / Right | Cycle views while preserving the filter |
| Tab / Shift+Tab | Cycle Open / Kill |
| Enter | Open, or begin explicit Kill confirmation |
| Alt+R | Request bounded passive refresh |
| Escape / Ctrl+G | Close the picker |

The view ring is Open → Attached → All → Local → configured remote hosts.
Open/Attached use different evidence; neither grants a close handle or provider
lifecycle authority. Unknown and unavailable state stays visible.
See [usage](docs/guides/usage.md) for detailed views, selection and confirmation.

```sh
rofi-tmux-plus inventory --json
```

The CLI command is an explicit fresh inventory. The picker instead consumes
cached prepared views; browsing starts no collection or service and does not
fall back to a fresh read on failure. Exact lifecycle argv and guards are in
[Tmux Session v1](docs/reference/tmux-session-v1.md).

## Ownership and operation

Tmux Observer owns passive facts, prepared fleet/desktop composition and
independent action validation. Mesh supplies configured routes/cached delivery.
Tmux Plus owns presentation and explicit intent. These remain generic tmux tools;
Agent Plus independently enters native provider managers without Tmux.

See [component boundaries](docs/reference/architecture.md) and
[Mesh selection](docs/guides/mesh-integration.md) for runtime and deployment ownership.
Source, installed, graphical, recovery and physical acceptance remain separate.

## Develop and read further

```sh
PATH="$PWD/.venv/bin:$PATH" ./scripts/check
```

Create that environment with the exact producer and declared tools first using
[development](docs/maintenance/development.md). The [documentation index](docs/README.md) links maintained guides and contracts.
Shared presentation lives at [Fleet Plus](https://dev.byebyebryan.com/fleet-plus/).

Current guides are organized in the [documentation index](docs/README.md).
