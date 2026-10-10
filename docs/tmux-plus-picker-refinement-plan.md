# Tmux Plus picker refinement plan

Status: Implemented in `0.6.0`. See the historical
[candidate validation](tmux-plus-picker-validation.md) and
[release review](tmux-plus-0.6.0-release.md) for the managed rollout boundary.
Baseline: `0.5.1`, source
`5b02f84fc0e18215426c652acfa942cdbae4cbcb`. Date: 2026-10-06.

Make Tmux Plus more convenient for daily manual session management: reopen in
the last useful context, provide Open and Attached views, and update remote
rows without coupling every desktop observation to a network refresh. Measure
the current path before changing it and compare the resulting behavior on
the same hosts. This pass keeps Tmux Plus independent of the Agent Observer
and Agent Plus migration.

## Scope and implementation order

| Step | Deliverable | Acceptance |
| --- | --- | --- |
| 1 | Refresh timing harness and baseline | Identify foreground, network, collection, and display delays separately. |
| 2 | Remembered context and initial selection launcher | Reopening restores the view and last successfully opened session through the real Rofi invocation. |
| 3 | Open and Attached views | Membership, uncertainty, empty views, and changing selections follow the rules below. |
| 4 | Independent viewer and owner refresh | Viewer renewal performs no SSH inventory calls while owner facts remain fresh. |
| 5 | Progressive host publication | A completed healthy host appears while another peer is still refreshing. |
| 6 | Source, managed, installed, and operator acceptance | Record timing changes and verify the selected implementation on Snap and Starship. |

Persistent SSH watch, native tmux event subscriptions, provider activity labels,
and a resident daemon belong to a later pass. This pass establishes the state,
freshness, and measurement boundaries that such a watch would need.

## Remembered context

Store one preference record per logical viewing endpoint under
`${XDG_STATE_HOME:-~/.local/state}/rofi-tmux-plus`. Use a versioned, bounded
record and the existing owned-file and atomic-write conventions. Preferences
are local UI hints and supply no lifecycle authority.

| Value | Update rule | Restore rule |
| --- | --- | --- |
| Last view | Explicit Left/Right navigation and a successful Open | Normalize against the current view ring and Host Mesh catalog. |
| Last successfully opened session | After ordinary Open successfully focuses or launches a viewer | Select exactly one visible row with the same `(hostId, serverGeneration, sessionId, createdAt)`. |

Session name, row index, route, attachment count, and Mesh revision are not
bookmark identity. Read current row metadata when an action occurs. A rename
preserves the bookmark; a replacement server or reused session ID does not.
Open success means the existing public command's focus/spawn result, rather
than a new promise that remote attachment completed.

Each new dialog starts with an empty filter, Open selected, and no pending
confirmation. The saved view is restored even when Open or Attached is empty.
A removed host falls back to All when remotes exist, otherwise Local. Missing,
corrupt, unsupported, or unsafe preferences use the same default. If the saved
session is absent or outside the chosen view, select the first eligible row.
Keep the bookmark until a later successful Open replaces it; do not perform
extra discovery or switch views to find it.

Apply the bookmark only on initial preparation. Later refreshes preserve the
current highlighted identity. An explicit view change preserves the filter
and selects the first eligible row, matching the existing navigation behavior.
If a refresh removes the highlighted row from the view, select the first
eligible row or the nonselectable empty state; an invisible former row must
never become an Enter target.

Arrow-only highlighting followed by native Escape or Ctrl+G does not update
the bookmark. Failed Open, Kill, background refresh, diagnostic commands, and
preference write failures do not replace the last successfully opened session.
An explicit view change remains remembered after cancellation. Preference
failure must not turn a completed Open into a reported lifecycle failure.

Add a `rofi-tmux-plus-rofi` launcher modeled on Agent Plus's launcher. Prepare
the initial frame once, pass its selected index through Rofi's `-selected-row`,
and serve that exact private frame to the first callback. Remove the temporary
frame on exit. Subsequent callbacks keep the existing script boundary. Direct
script-mode invocation remains usable and restores the view; guaranteed initial
row restoration uses the launcher. Update the managed Mod+G invocation and
launcher link during the implementation rollout.

## Added views

Use this wrapping Left/Right ring when remotes exist:

```text
Open -> Attached -> All -> Local -> configured remote hosts
```

With no remote host, use `Open -> Attached -> Local`, retaining the existing
collapse of redundant All and Local. Host order remains authoritative Host Mesh
order. Open and Attached are global views and remain in the ring when empty.
The first invocation without preferences still starts in All or Local.

| View | Membership |
| --- | --- |
| Open | Fresh caller-local viewer evidence with `state=open` and `confidence=confirmed` or `matched`, joined to the exact current owner/session context. |
| Attached | Fresh successful owner inventory with an integer `attachedClients > 0`; clients may be on any endpoint. |
| All, Local, and host views | Existing session inventory, including clearly historical rows from unavailable hosts. |

Use Attached as the UI label. It describes tmux attachment and makes no claim
about agent work, waiting, or completion. Open describes this desktop and
includes the qualified manual SSH viewer hint. Matched rows retain their
`open here?` presentation and gain no verified close handle or extra action.

Preserve existing recency order, two-line rows, Open/Kill action cycling, and
guarded confirmation. Compute view membership only from the exact presentation
snapshot; Left/Right and Tab callbacks perform no discovery, desktop scan,
tmux inventory, or SSH work. Timed callbacks may adopt a newly published cache
frame, preserving the current filter, action, and surviving selection.

Each filter excludes rows whose required evidence is expired, incomplete,
conflicting, or unavailable; a failed desktop scan alone does not invalidate
fresh Attached membership. Distinguish an authoritative empty view from
checking or unknown membership: for example, `No open viewers here` versus
`Checking viewer presence` or `Viewer presence unknown; check All`. Show a
bounded uncertainty notice when only some hosts or observations are unknown.
All and concrete host views keep those rows available for inspection.

An entered Kill confirmation freezes its original reference and target. View
membership changes cannot replace that target; confirmation retains its current
live revalidation and duplicate-attempt guard.

## Refresh timing baseline

Add an opt-in `scripts/measure-refresh` harness and internal timing hooks. Keep
normal public JSON and Rofi protocol output unchanged. Record bounded timing
events in a private diagnostic artifact; use monotonic durations and include
the source version, endpoint, host count, session count, and cache condition.
Do not record pane contents, provider transcripts, credentials, or full commands.

Measure these boundaries separately:

- Launcher preparation and first prepared frame, including Host Mesh load,
  local tmux collection, preference lookup, and cached rendering. Measure the
  actual Rofi surface separately during graphical acceptance.
- Each remote route attempt from SSH launch to completed output, route-health
  reporting, parsing, and cache publication; include retries and fast versus
  compatibility collection. Measure remote collection separately where possible
  so total SSH duration is not mistaken for handshake time.
- Desktop scan and viewer-cache publication independently of owner collection.
- Time from a host's completed result to its first rendered updated row, plus
  the selected row and filter continuity across those frames.
- SSH attempts, helper processes, bytes, and repeated local/Host Mesh probes
  during a fixed idle picker window.

Compare warm retained cache, cold private cache, forced Alt+R, viewer-only
renewal, and a healthy peer alongside a delayed or failed peer. Use isolated
cache and preference roots. Collect 20 healthy warm samples and at least five
cold samples per endpoint; report sample counts, median, observed p95 where
there are enough samples, and maximum. Record whether the existing SSH
configuration actually reuses connections, without changing that configuration.
Use controlled delayed/failing peers for reproducible fault tests, then compare
normal remote reads from both endpoint perspectives.

## Independent refresh and freshness

The current finite refresh combines owner inventory and viewer observations.
The ten-second viewer TTL can therefore request all-host network work even
when the thirty-second owner refresh interval has not elapsed. Split the
finite owner and viewer jobs, with separate locks, request markers, deadlines,
and retry cooldowns. Repeated callbacks and concurrent picker windows must
deduplicate each kind of job independently.

Viewer renewal uses the local desktop/process scan and already available owner
rows. Give it its own bounded scan deadline. For known positive observations,
start renewal after seven seconds and expire at ten seconds; failed or unknown
observations retry on a bounded ten-second cadence. Preserve strict expiry and
desktop-context invalidation. Local scan success does not advance owner clocks.
Use a one-second native callback cadence while monitoring these jobs and clocks,
and test Rofi's previous-frame timeout ordering across view changes.

Track each owner's last successful observation and last attempt separately.
Use the existing configured owner refresh interval as the owner freshness
budget and schedule renewal with a bounded lead time chosen from measurements.
Fresh desktop evidence cannot refresh an old remote attachment count or session
reference. Manual-shell qualification still requires current positive owner
attachment evidence. A failed owner read retains historical facts for All and
host views while positive Open/Attached membership becomes unknown. Renewing
one host never makes another host look fresh.

Alt+R requests both refresh kinds and returns a cached frame promptly. Owner
completion can request a coalesced viewer check for changed references or facts.
Post-mutation reconciliation continues to refresh only the affected owner.
Keep native lifecycle revalidation, per-host isolation, route reporting,
process/output bounds, and bounded retry behavior intact.

## Progressive host publication

The public inventory command continues to return one complete bounded JSON
document. Add progressive completion handling only to the private picker
refresh path. Reuse the existing validated single-host merge boundary; the
complete-set `merge` operation must not be fed partial results.

Validate each completed host row and the pinned Host Mesh revision before its
atomic cache publication. Preserve peer rows and their clocks. Allocate operation
sequence tokens before collection and fence pending results when a newer
observation or mutation supersedes them; completion timestamps alone cannot
prove which facts are newer. Discard results from an obsolete Mesh revision.
Readers adopt complete private cache frames rather
than observing partially written rows. Keep the finite batch deadline and
publish terminal refresh status after all owned work finishes or is reaped.

A healthy host should become visible within one timed callback plus rendering,
with a target of two seconds after completion under normal desktop load,
independent of another host's remaining timeout. Local viewer renewal should
likewise finish without waiting for remote collection. Global refresh status
may still indicate another pending peer; retain useful completed rows.

## Implementation areas

| Area | Expected changes |
| --- | --- |
| `rofi.py` and presentation cache | View ring, membership, empty states, saved initial state, and selection continuity. |
| New preference and launcher modules, `bin`, and packaging | Owned preference storage, initial frame preparation, and console launcher. |
| `picker_model.py` and `viewer_cache.py` | Independent jobs, renewal scheduling, context checks, and timer coordination. |
| `remote_cache.py` and `inventory_service.py` | Owner clocks and private progressive publication; public aggregate semantics retained. |
| `remote_inventory.py` and timing harness | Opt-in measurements; optimize collection only where the baseline identifies avoidable cost. |
| Chezmoi | Launcher link, Mod+G command, exact release pin/checksum, managed assertions, and deployment ledger. |

Version private cache formats when their semantics change. Reject incompatible
old cache state and rebuild through bounded refresh; leave durable preferences
separate from disposable caches. Host Mesh v1 and the public Tmux Session v1
canonical bundles remain compatible. Borrow the established design patterns
without importing another project's internals or reading its private state.

## Acceptance and rollout

Add focused regressions for preference fallback, rename versus replacement
identity, successful versus failed actions, initial-frame reuse, empty and
uncertain views, membership changes during Kill confirmation, filter and
selection continuity, timer ordering, independent expiry, job deduplication,
stale revisions, out-of-order host results, and timeout cleanup.

Prove zero SSH inventory calls for viewer-only renewal while owner facts are
fresh, and for every Left/Right or Tab callback. A delayed-peer fixture must
expose the healthy row before that peer completes. A fresh local scan must not
restore Open/Attached membership using expired remote facts. Diagnostic and
failed operations must leave production preferences and SSH history intact.

Run the repository source gate and contract sync, then the exact suite candidate
gate. Recheck the current managed tuple and Observer/Agent Plus override before
rollout; preserve those selections. Use the current chezmoi deployment ledger
for targets, scoped deployment, and acceptance records. The expected targets
are Snap and Starship; Carbon remains outside this pass.

Compare source and installed files, modes, and launcher links separately.
Exercise the installed checks and record before/after timing deltas. Through
the actual managed Mod+G command, verify reopen selection, rename continuity,
missing rows, page changes followed by Escape, filter preservation, Open versus
Attached membership, remote refresh, and unchanged Kill styling/confirmation
on disposable owned sessions. Live graphical checks require an available
operator test window and must preserve ordinary sessions.

Complete the source and timing work before release review. Record operator
visual/focus acceptance separately from automated checks, and retain the prior
artifact and launcher route for rollback.

## Related records

- [Current product and interaction design](DESIGN.md)
- [Public Tmux Session v1 contract](TMUX_SESSION_V1.md)
- [Agent Plus remembered context and initial selection design](../../rofi-agent-plus/docs/agent-plus-picker-navigation-plan.md)
- [Managed Rofi Plus deployment ledger](https://github.com/byebyebryan/dotfiles/blob/5ff8b412a45b69cac64b8eb356444e6cf0705012/docs/rofi-plus-status.md)
