# Product and interaction design

Current behavior for Tmux Plus **0.12.0a2**, Observer **0.6.0a2** and Mesh
**0.1.0a7**, recorded 2026-10-10. This guide describes presentation and user intent.
[Mesh selection](mesh-integration.md) and
[managed operations](https://github.com/byebyebryan/tmux-observer/blob/main/docs/managed-operations.md)
own exact installed selection. [Product design history](design-history.md)
preserves the earlier P6–P11 implementation milestones and retired cache model.

## Product boundary

Tmux Plus is a searchable picker for sessions on the local default tmux server
and configured logical hosts. It remains useful for manual tmux management.
Agent Plus consumes its generic Tmux Session v1 CLI; provider discovery, task
state and resume policy belong to that consumer.

The UI owns labels, ordering, view membership, filter/caret, selection,
bookmarks, confirmation and feedback. Observer supplies validated prepared
facts and a separate action client. Mesh supplies host authority and reusable
state networking. See [component boundaries](observer-boundaries.md) and
[Observer runtime architecture](https://github.com/byebyebryan/tmux-observer/blob/main/docs/runtime-architecture.md).

## Views and session rows

The wrapping view ring is:

```text
Open → Attached → All → Local → remote hosts in catalog order
```

With no remote hosts, All and Local collapse to Local. A first invocation
defaults to All when available, otherwise Local. Empty Open/Attached views and
empty or unavailable configured hosts retain their place in the ring;
activity and availability do not reorder hosts.

| View | Membership |
| --- | --- |
| Open | Fresh successful owner facts plus confirmed or qualified viewer evidence on this desktop |
| Attached | Fresh successful owner facts with positive native tmux client counts |
| All / Local / host | Relevant sessions, including explicitly retained unavailable rows |

Open and Attached are independent: a remote client may establish Attached
without a local desktop viewer. Failed or unsupported viewer observation leaves
membership unknown. Unknown is distinct from a complete empty view. All or Local
keeps sessions available for inspection.

Session rows use two lines: the session name, then host/path/window/status/activity
metadata. Concrete host views omit the redundant host label. Search metadata
retains the logical host, complete path, name, current window and status. The typed
reference in `ROFI_INFO` supplies selection identity; rendered text does not.

`open here` denotes supported confirmed presence; `open here?` denotes qualified
matching or a retained local association with current native inputs. Manual
remote SSH shells can qualify when unique title/owner, route/process and positive
attachment evidence agree. Neither label supplies a verified close handle.
Activity age is tmux's native timestamp, not source freshness or agent progress.

Rofi row tokens convey confidence and action state:

| Row | `active` | `urgent` | Selectable |
| --- | --- | --- | --- |
| Fresh open-here or qualified session | yes | no | yes |
| Fresh attached/detached session | no | no | yes |
| Retained session from an unavailable host | no | yes | yes |
| Kill-mode session | yes | yes | yes |
| Empty scope | no | no | no |
| Empty concrete scope with unavailable host | no | yes | no |
| Kill-confirmation action | yes | yes | yes |

The managed theme owns colors. Refresh activity and old activity timestamps do
not create warning tokens. Recovery clears unavailable treatment on the next render.

## Navigation and remembered context

| Key | Behavior |
| --- | --- |
| Up / Down | Native row selection |
| Left / Right | Wrap through views; keep filter and select the first eligible row |
| Tab / Shift+Tab | Cycle Open/Kill forward or backward |
| Enter | Run the selected Open intent or enter Kill confirmation |
| Alt+R | Request bounded passive reconciliation |
| Escape / Ctrl+G | Native cancel from every state |
| Ctrl+B / Ctrl+F | Move the text cursor |

The prompt names the host scope. The persistent message shows the selected Enter
action, both choices and the Tab hint. Bounded notices follow separately.
Malformed action state blocks Enter with a visible error.

Preferences are bounded endpoint-local XDG state. Explicit view changes are
remembered. Only a successful Open replaces the last-used session reference;
highlighting, cancellation, failure and Kill do not. A completed focus or terminal
spawn is the compatibility meaning of Open success, not proof of remote attachment.

The bookmark identity is `(hostId, serverGeneration, sessionId, createdAt)`.
Rename preserves it; server replacement and ID reuse do not. A removed remembered
host falls back to All or Local. An absent bookmarked row uses the first eligible
row without switching views or performing extra discovery.

Each dialog starts with an empty filter, Open selected and no pending confirmation.
The launcher prepares a frame and passes its selected row to Rofi. Bookmark
restoration happens once; later updates preserve the currently highlighted
complete identity when visible. Explicit view changes reset selection.
Direct script invocation can restore the view but does not guarantee the initial row.

## Prepared updates and explicit refresh

Startup and browsing consume the prepared local Fleet v1 view and one owned
read-client watch. Timer, navigation and action-cycle callbacks adopt validated
prepared facts without tmux, SSH or desktop collection. Presentation snapshots
are private UI state; they are not observation caches or action authority.

Owner facts, native local bindings, remote desktop evidence and watch health have
independent expiry. Every adopted/rendered frame rechecks scope and freshness.
A heartbeat renews no source fact. Quiet renewal can update leases without a UI
redraw; material changes, expiry, ticket results and recovery wake adoption.

An expired or unavailable owner can retain historical rows in All/host views.
Positive Open/Attached membership is revoked. Viewer failure alone does not
invalidate healthy native inventory. Complete-empty, unknown, warming and failed
states keep distinct presentation.

Alt+R preserves filter, selected reference and action, requests fixed owner/desktop
sources and tracks an incarnation-bound refresh ticket. Its notice clears on a
terminal ticket outcome even when rows are unchanged. An incomplete or failed
refresh cannot be presented as complete success. Post-action reconciliation
targets the affected owner and cannot repeat the native action.

## Open and guarded Kill

Open passes the exact selected target to Observer's action client. That client
revalidates the route, native identity and any focus candidate. It can focus a
unique compatible viewer or launch the configured terminal. Ambiguous viewer
evidence is rejected under the picker policy; it does not choose the first title
match. An uncertain dispatch is reported once and never automatically repeated.

Kill requires current host evidence before entering confirmation. The confirmation
freezes the complete reference and observed-name guard, names host/session and
attachment impact, and selects Cancel initially. Updated observations cannot
retarget it. Left/Right do nothing during confirmation. Escape closes without an
uncommitted action; Cancel returns to Open browsing.

A failed attempt retains an attempted guard, preventing another Enter from
dispatching the same Kill. Cancel and explicitly selecting Kill again create a
new intent. Success returns to Open. The action client independently revalidates
the exact target before mutation; cached state never authorizes deletion.

The browse surface exposes Open/Kill. CLI consumers retain `create`, `rename`,
`viewers` and `close-viewer` alongside `open` and `kill`.
The [Tmux Session v1 contract](TMUX_SESSION_V1.md) owns argv, JSON, typed errors,
native option guards and effect semantics. Verified close has stronger checks
than display presence.

## Configuration and installation

Optional configuration lives at
`${XDG_CONFIG_HOME:-~/.config}/rofi-tmux-plus/config.toml`:

```toml
schema_version = 1
terminal = ["ghostty"]
refresh_seconds = 30
attach_timeout_seconds = 60
```

`terminal` is an argv prefix, never a shell command. The action client appends
the attachment invocation. `attach_timeout_seconds` bounds the programmatic
first-client gate. `refresh_seconds` remains an accepted compatibility setting;
the prepared picker does not use it to change owner polling, leases or Mesh
cadence. Observer services own those schedules.

Unknown keys, malformed values and a present invalid file are visible errors.
The launcher validates its packaged native Rofi mode and supported binary tuple,
prepares initial selection, owns the watch and stops only its owned children.
Unsupported or drifted native integration produces a bounded error display.

Prepared browsing requires already running owner/fleet services and a configured
Mesh source. The generic fleet command defaults to legacy; managed controls
explicitly select Mesh. Package installation alone starts no service.
Explicit CLI inventory remains fresh and is usable independently of prepared
service health; remote fresh/lifecycle operations retain Host Mesh v1 routing.

## Validation and limits

[Development](development.md) explains source setup and checks.
[Current Mesh evidence](mesh-integration.md) records exact headless frames/callbacks,
native passivity, resources, installed recovery and paired rollback.
Those passes do not establish graphical appearance/input or physical suspend.
Earlier [migration](observer-migration.md) and [release records](README.md#historical-records)
retain their own graphical artifact/endpoint scope.

Native tmux events and broader compositor/terminal support require separate
producer and consumer acceptance. Open/Attached remain display views and do not
infer agent work, completion, provider identity or lifecycle authority.
