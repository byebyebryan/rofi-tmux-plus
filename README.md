# rofi-tmux-plus

`rofi-tmux-plus` is a Rofi picker and manager for local and remote tmux
sessions. It presents one mixed session inventory, uses `rofi-ssh-plus` for
logical hosts and SSH routes, and exposes generic tmux lifecycle operations
for `rofi-agent-plus`.

The picker consumes Tmux Observer's prepared local views and watch updates. The
reviewed [component boundary note](docs/observer-boundaries.md) defines ownership:
native observation, networking, desktop association and action clients retain their
own contracts; Tmux Plus retains presentation and user intent. The
[0.10.0a2 fleet performance acceptance](docs/fleet-performance.md) records the
current Observer 0.4.0a1 pair, Snap graphical checks and scoped managed deployment
on Snap and Starship. The
[0.8.0a1 release candidate](docs/tmux-plus-0.8.0a1.md) delegates actions as well
as reads to the pinned Observer package. Its exact installed/native, Snap picker,
resource, publication and managed recovery/paired rollback gates pass in their
recorded scopes.

Tmux Session Contract v1 provides strict versioned JSON inventory across the
local default server and compatible Host Mesh remotes, plus safe `open`,
`create`, `rename`, and `kill` operations on either side. Remote inventory is bounded,
nonce-authenticated, and reports revisioned route health through the public
SSH Plus command. It always targets the local default tmux server; test-only
isolated sockets are not a public CLI feature. It also publishes deterministic
producer fixtures for consumers.

The native Rofi browse surface has Open and guarded Kill actions. Observer's
owner and desktop collection runs independently of the picker; startup and
browsing read prepared facts without native collection or service activation.
Public `inventory --json --with-viewers` remains an explicit fresh read and
adds the caller endpoint and per-session `open`, `none`, or `unknown` state;
plain inventory stays unchanged. These observations do not provide viewer
handles or change guarded actions. The public lifecycle contract continues to
expose `create` and `rename` for CLI consumers.

Version `0.5.1` also recognizes a manually opened remote SSH shell, such as
`kitty -e ssh snap`, as qualified `open?` when one window has the exact current
session/owner title, a live terminal SSH process to the selected route, and a
positive owner attached-client count. The supported shell argv is `ssh HOST`,
optionally with `-t` or `-tt`, without a remote command. Duplicate matches,
conflicting metadata, incomplete scans, and pending sessions remain unknown.
This display hint supplies no verified close handle and does not alter batch
eligibility. No additional SSH request or provider lifecycle hook is used.

For development without installing the console script, inventory is an explicit
fresh read. The picker additionally requires the accepted Observer installation,
prepared services and packaged native mode for the supported Rofi binary:

```sh
PYTHONPATH=. ./bin/rofi-tmux-plus inventory --json
./bin/rofi-tmux-plus-rofi -show tmux-plus -modes tmux-plus \
  -kb-custom-1 Alt+r -kb-custom-2 Right -kb-custom-3 Left \
  -kb-custom-7 Tab -kb-custom-8 ISO_Left_Tab \
  -kb-element-next "" -kb-element-prev "" \
  -kb-accept-custom "" -kb-delete-entry "" \
  -kb-cancel Escape,Control+g \
  -kb-move-char-forward Control+f -kb-move-char-back Control+b \
  -eh 2
```

The launcher validates and loads the native `tmux-plus` mode, with the callbacks
above. See [migration](docs/observer-migration.md) and the
[current repair](docs/tmux-plus-0.7.0a2.md) for their separate acceptance scopes.
Open is the initial action; Tab advances to Kill and Shift+Tab reverses
the ordered action cycle, with wraparound. The prompt shows the host scope;
the persistent message shows `Enter:` with both actions, highlights the
selected one, and separates `Tab: Cycle actions` with a divider. Notices follow
after a blank line. Enter opens or begins a kill confirmation for
the session highlighted at that moment. It never acts on Tab. Right and Left
wrap `Open`, `Attached`, `All`, `Local`, and Host Mesh remote scopes. With no
remote, the ring is `Open`, `Attached`, `Local`. `Alt+R` requests bounded
background owner and viewer refreshes. Escape and `Ctrl+G` use Rofi's native cancel action and
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

Each model render is retained in a private content-addressed presentation cache.
Left/Right and Tab/Shift+Tab adopt current validated prepared facts and persist the
renewed frame without native collection or action dispatch. Pending confirmations
retain their exact frozen presentation/target. The cache keeps the newest 256
owned snapshots and fails closed when the required snapshot is missing or corrupt.
Updates retain the active action and stable typed session selection when possible.

Tmux Plus `0.6.0` remembers the last view and last successfully opened
session per viewing endpoint. The launcher prepares one frame and uses Rofi's
initial selection option; direct script invocation restores the view but does
not guarantee the initial highlighted row. Bookmarks match the complete session
reference, survive renames, and never supply lifecycle authority. Each new
dialog starts with an empty filter and Open action.

Open contains fresh confirmed or qualified viewers on this desktop; Attached
contains fresh owner observations with positive tmux client counts, including
clients elsewhere. Both remain present when empty and explain unknown
membership. Observer keeps owner and desktop receipts independent. Its watch
delivers material changes and relevant expiry; quiet renewal does not force a UI
redraw. Timer adoption, navigation and action cycling use prepared state without
foreground tmux/SSH collection. Explicit Alt+R tracks a bounded refresh ticket.

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
- [Observer and action-client boundaries](docs/observer-boundaries.md)
- [Picker refinement plan](docs/tmux-plus-picker-refinement-plan.md)
- [Candidate validation and rollout](docs/tmux-plus-picker-validation.md)
- [Tmux Session Contract v1](docs/TMUX_SESSION_V1.md)
- [Host Mesh Contract v1](https://github.com/byebyebryan/rofi-ssh-plus/blob/main/docs/HOST_MESH_V1.md)

The component ownership is:

```text
rofi-ssh-plus ──> Observer fleet/desktop reader ──> Tmux Plus UI
  host/routes              ^
                      tmux-observer
                     native owner facts

Tmux Plus UI ──> C5 action client ──> guarded native / terminal / window adapters
Tmux Plus public CLI ──> Observer legacy action facade
                              ^
                        Agent Plus consumer
```

Components consume public versioned contracts/client facades. They do not import
another repository's private implementation or treat observation as action authority.
