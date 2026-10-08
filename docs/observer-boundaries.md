# Tmux Plus as an observation and action client

Reviewed: 2026-10-08. Design-only follow-up to the accepted Observer read
migration and the [0.7.0a2 browse renewal repair](tmux-plus-0.7.0a2.md).
No runtime behavior or public CLI contract changes in this documentation pass.

The authoritative cross-component design is Observer's
[component boundaries](https://github.com/byebyebryan/tmux-observer/blob/main/docs/component-boundaries.md),
with its [source review](https://github.com/byebyebryan/tmux-observer/blob/main/docs/component-boundaries-review.md).
This note owns the consumer responsibilities and migration work in this repository.

## UI and compatibility facade

The Rofi picker should be presentation and user intent. The
`rofi-tmux-plus` public CLI is also a compatibility client for Tmux Session v1;
that command's inventory/lifecycle API does not make those implementations UI.

| Concern | Owner |
| --- | --- |
| Native tmux identity, inventory and attachments | Passive host-local native reader and owner service |
| Host catalog/routes and remote owner observation | Host Mesh authority plus separate networking/read client |
| Local windows/process evidence and session matching | Optional desktop observation adapter consuming accepted native inputs |
| Prepared snapshots/watch and passive refresh tickets | Shared prepared reader coordinator |
| Native lifecycle, terminal launch and window focus/close | Separate action client and action adapters |
| Labels, sort, views, selection, filter, bookmarks and confirmation | Tmux Plus |

The UI reads a validated prepared view and owns one read-client watch. It never
starts a publisher, invokes native/process/compositor discovery, opens observation
SSH links or retries an action. Explicit Refresh is a bounded reconciliation
request with a terminal ticket outcome. Post-action reconciliation targets only
the affected owner and remains separate from action success.

Open requires fresh owner and this desktop's association evidence. Attached
requires fresh positive native attachment counts. Confidence and uncertainty
come from contracts; formatting and membership projection belong here.
Activity age formats native timestamps; it is not source-health age or agent state.

Preserve the last view and last successfully opened complete reference. Failed
actions, cursor movement and Cancel do not change that bookmark. View/action
cycling reads current prepared facts without native collection or action-client
construction. Quiet renewals must not leave an expired presentation as the basis
for new warning rows. Pending confirmations remain frozen on their exact targets.

## Action client

The UI passes explicit intent and operation-relevant guards. The action client
selects/revalidates host routing and the exact native target, and independently
inspects any window it will focus or close. Create needs explicit host/name/cwd/
command, not an existing row. An unavailable prepared reader is not a blanket
ban on independently valid CLI actions.

`open here?` means qualified matching; it remains useful and display-only.
Successful focus does not upgrade it to a verified observation or close handle.
The refined action contract rejects ambiguous first-title focus. That is a
documented behavioral tightening requiring a legacy mapping, not a promise
that moving today's code automatically preserves every ordinary-Open edge case.
Strict verified-viewer and close guards must survive extraction.

Separate session/native effect, local spawn, completed attachment, focus and
transport uncertainty. Preserve the v1 meaning of `terminalLaunched`: successful
local spawn, not proof of completed remote attachment. A refresh ticket or request
ID supplies no action idempotency. Confirmation after an uncertain outcome cannot
be treated as authorization to repeat the previous action automatically.

## Current implementation and extraction work

Prepared browsing and the default fresh inventory facade already delegate to the
accepted Observer artifact. [Observer imports](../rofi_tmux_plus/observer_client.py)
still load prepared, direct and Mesh modules together; future client facades should
separate those entry paths while preserving accepted artifact/contract validation.

[LifecycleService](../rofi_tmux_plus/lifecycle_service.py),
[local lifecycle](../rofi_tmux_plus/lifecycle.py),
[remote lifecycle](../rofi_tmux_plus/remote_lifecycle.py) and
[viewer inspection/close](../rofi_tmux_plus/viewer_service.py) still own actions
here. Mixed old observation/cache modules also remain. Trace active CLI/test seams
before removal; their presence alone does not establish that browse executes them.

The producer sequence is B0 contract freeze, B1 native client-association profile,
B2 desktop matching and B3 action client. B4 then migrates this repository:

1. Freeze fresh/cached read semantics and old CLI action responses with independent
   compatibility cases; record intentional confidence/focus behavior changes.
2. Consume narrow prepared and action facades; keep fresh inventory explicitly fresh.
3. Delegate CLI actions to the accepted write client, preserving exact references,
   route guards, errors, exits and verified-viewer policy.
4. Remove superseded active collectors/scanners/transport loops and lifecycle
   implementations only after their replacement gates pass.
5. Recheck remembered context, quiet renewal, real expiry, filter/caret, watch
   recovery and frozen confirmation with the selected native Rofi integration.

Source/import checks do not establish graphical or installed acceptance. Native
GUI checks use Starship; Snap remains in active use. Exact artifacts, managed
selection, rollback and installed checks are a later independent B5 gate.
No new package names, commands, schema versions or deployment are selected here.
