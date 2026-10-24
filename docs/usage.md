# Tmux Plus usage

Use the accepted native bundle and independently running prepared services.
For installation prerequisites see [getting started](getting-started.md).

## Browse sessions

| Key | Behavior |
| --- | --- |
| Up / Down | Select a row |
| Left / Right | Cycle views, preserving the filter |
| Tab / Shift+Tab | Cycle Open and Kill |
| Enter | Open, or begin Kill confirmation |
| Alt+R | Request bounded passive refresh |
| Escape / Ctrl+G | Close the picker |
| Ctrl+B / Ctrl+F | Move the text cursor |

The view ring is **Open → Attached → All → Local → configured remote hosts**.
Without remotes, All and Local collapse to Local. Empty and unavailable
configured hosts retain their place in the ring.

| View | Meaning |
| --- | --- |
| Open | Fresh confirmed or qualified viewer presence on this desktop |
| Attached | Fresh positive tmux client counts, including clients on other desktops |
| All / Local / host | Session inventory, with retained unavailable rows marked explicitly |

`open here?` is qualified display evidence. Open and Attached supply no verified
close handle or provider lifecycle authority. Unknown membership remains explicit;
use All or Local to inspect sessions excluded from a filtered view.

Each new dialog starts with an empty filter and Open selected. The launcher
restores the remembered view and last successfully opened complete session
reference once at startup. Later updates preserve the current selection.
Kill confirmation freezes its exact target and starts on Cancel.

See the [product and interaction guide](DESIGN.md) for row states, freshness,
selection, confirmation and failure behavior.

## Use the public CLI

With the installed public command:

```sh
rofi-tmux-plus inventory --json
rofi-tmux-plus inventory --json --with-viewers
```

Inventory is an explicit fresh operation. The optional viewer extension reports
this caller's `open`, `none` or `unknown` evidence without changing owner facts
or providing action handles. The public CLI also exposes `open`, `create`,
`rename`, `kill`, `viewers` and `close-viewer`; their exact argv, guards and
JSON are defined by [Tmux Session v1](TMUX_SESSION_V1.md).

Remote fresh inventory and lifecycle routes consume Host Mesh v1 through the
public SSH Plus command. They target the default tmux server, preserve complete
identities and independently validate actions. Prepared Mesh reads are a separate
interface; an outage never silently changes a cached read into fresh collection.

## Launch the picker

Use the accepted Observer installation, running prepared services and packaged
native mode for the supported Rofi binary. The source launcher invocation is:

```sh
./bin/rofi-tmux-plus-rofi -show tmux-plus -modes tmux-plus \
  -kb-custom-1 Alt+r -kb-custom-2 Right -kb-custom-3 Left \
  -kb-custom-7 Tab -kb-custom-8 ISO_Left_Tab \
  -kb-element-next "" -kb-element-prev "" \
  -kb-accept-custom "" -kb-delete-entry "" \
  -kb-cancel Escape,Control+g \
  -kb-move-char-forward Control+f -kb-move-char-back Control+b \
  -eh 2
```

The launcher verifies the native mode and Rofi binary tuple, prepares the initial
selection and owns its watch/process cleanup. Unsupported or drifted integration
produces a bounded error display. Package installation and service startup are
separate operational steps.

For checkout setup and the source gate, use [development](development.md).
The [documentation index](README.md) links current contracts, configuration,
selection and historical release evidence.
