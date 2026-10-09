# Tmux Plus as an observation and action client

Reviewed: 2026-10-08. B4 source implementation follows the accepted Observer read
migration and the [0.7.0a2 browse renewal repair](tmux-plus-0.7.0a2.md).
Source candidate 0.8.0a1 remains unselected until its independent runtime gates pass.

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

Prepared browsing and the default fresh inventory facade delegate to the pinned
Observer artifact. [Observer imports](../rofi_tmux_plus/observer_client.py) verify
all three package payloads, then load prepared, direct, Mesh, pure contracts and
actions independently on demand. Importing/constructing browse clients and lazy
action objects works with collectors, network loops, desktop scans and action
implementations blocked.

[LifecycleService](../rofi_tmux_plus/lifecycle_service.py) now lazily delegates the
published CLI's old methods and argv to Observer's legacy action facade. Its
separate `ActionService` maps the picker Open/Kill intent into closed C5 requests
and independently binds replies to the exact request/reference. Frozen names
remain Kill guards. Uncertain actions are surfaced once and never redispatched.
The legacy CLI is not reencoded into the new C5 request size/argv limits.

[Fresh inventory](../rofi_tmux_plus/inventory_service.py) delegates the optional
viewer profile and matching to Observer's explicit direct job. The caller endpoint
timestamp retains wall-clock milliseconds; prepared leases retain their own boot
clock. Optional desktop failure yields unknown presence without replacing healthy
native inventory. It does not activate an owner or prepared service.

Superseded native readers, SSH/process discovery, desktop inspection, lifecycle
implementations and old finite-refresh caches are excluded from the product
package. A [frozen test baseline](../tests/reference_frontend/README.md) keeps their
historical regressions available. Those cases validate the baseline, not the
replacement. Active consumer/C5/import cases, unchanged public fixtures, producer
tests and installed native/graphical evidence supply the replacement gates.

Private `_picker-model`, `_refresh` and `_refresh-status` now use the prepared
reader. `_refresh` requests grouped reconciliation; its old detached native/SSH
worker and cache marker are retired. Private measurement of the historical
finite-refresh path explicitly imports the test baseline. These are private
interfaces, outside the seven-command Tmux Session v1 public contract.

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

Source/import checks do not establish graphical or installed acceptance. Current
native GUI checks use Snap; Starship remains in active use. Exact artifacts, managed
selection, rollback and installed checks are a later independent B5 gate.
No managed selection is implied by this source checkpoint. Public CLI flags,
schemas, output bounds, clean JSON errors and exits remain unchanged. Ordinary
Open's fresh unique focus/ambiguity tightening is the documented B3 delta;
verified-viewer/close guards remain owned by the separate action client.


The B4 frozen candidate passed 30 installed CLI cases across Snap and Starship,
then [21 native picker cases on Snap](evidence/2026-10-08-boundary-b4/snap-picker.json),
including remembered context, typing/caret, refresh, confirmation, local expiry,
read-only reconnect and cleanup. Ready/confirmation screenshots were inspected.
Warm frame p95 was 104.55 ms; observed launch surfaces were 175-177 ms. Callback
completion and surface polling do not establish compositor presentation timing.
The user moved future foreground testing to Snap while actively using Starship.

The [earlier Starship run](evidence/2026-10-08-boundary-b4/starship-watch-race-rejected.json)
passed eleven cases then raced the stopped subscription reconnect. The fault
injection now stops the owned delivery supervisor while leaving the renderer
running through the same expiry deadline; automatic reconnect remains a separate
case. No runtime code changed for this harness repair. The candidate is still
unselected: Observer normal-resource acceptance failed Snap at 5.1656% CPU against
5%, and subsequent optimization requires a new producer pin and frozen gates.


The next candidate pins Observer source `ae8df6e` and wheel
`ae52080fbfbc1d990af038fd93ef76909cb7b84e2a8b7067c53c062daa00d37d`.
The pin still verifies all 74 Python modules across the three package roots.
Its native read coalescing, prepared projection reuse, nested wire-check reuse
and bounded Niri socket reads preserve contracts, sampling cadence and budgets;
287 consumer source tests pass with that exact installed wheel. Frozen installed,
Snap graphical and actual-session resource gates remain separate from this pin.

The frozen `7a9155e` wheel (`92fe44eb…`) subsequently passes thirty installed CLI
cases across both hosts and
[twenty-one native Snap picker cases](evidence/2026-10-08-boundary-b5/snap-picker.json).
Ready and confirmation screenshots are inspected. Warm frame p95 is 100.50 ms;
observed launch surfaces are 177–179 ms. One owned explicit Refresh notice clears
in 246.47 ms, 50.23 ms after the service's terminal ticket. These isolated callback
and surface observations are not compositor presentation or two-host latency p95.
Owned processes/preferences are removed and prior focus restored. Starship has
no foreground testing under the current user constraint.

Observer's exact actual-session resource run still fails Snap's unchanged
five-percent CPU gate at 6.3647% (Starship 2.9777%); memory, native passivity and
cached-query deadlines pass. The user asks for the cause before selecting any
replacement CPU budget. Both packages remain unpublished and unselected, with
the existing managed pair retained.

The next producer repair reduces normal C1/C2 sampling to four native process
starts for up to 31 sessions, and reuses enclosing plain-tree checks within pure
validators. Nested semantics and independent byte ceilings remain; mutable Python
subclasses keep their full fallback. The new producer source passes 319 tests and
fourteen frozen collector cases on both hosts. Consumer source still passes 287
tests with the new exact pin. Normal resource acceptance now explicitly matches
each managed system-Python interpreter. Those new resource and frozen consumer
gates remain pending; preceding picker evidence retains its original pin.
