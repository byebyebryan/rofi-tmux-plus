# Tmux Plus picker candidate validation

Date: 2026-10-06. Candidate: `0.6.0a1`. Baseline: `0.5.1`,
`5b02f84fc0e18215426c652acfa942cdbae4cbcb`. The source pass is complete;
ordinary managed promotion remained a release step at this capture. No release was published,
no fleet pins or ordinary launchers were changed, and Agent Plus / Observer
pilot selections were preserved.

This is the historical candidate record. The subsequent `0.6.0` review and
managed promotion are recorded in [the release review](tmux-plus-0.6.0-release.md).

## Result

The candidate remembers an endpoint's last view and last successfully opened
complete session reference. It restores the initial row through a dedicated
launcher, preserves a bookmark through rename, and rejects replacement-server
or reused-session identity. Each new dialog starts with an empty filter, Open
action, and no pending confirmation. Failed Open and native cancellation do
not replace the bookmark; explicit view changes remain remembered.

The ring is Open, Attached, All, Local, then configured remote hosts. Local-only
mode retains Open, Attached, Local. Open requires fresh viewer evidence here
and fresh supporting owner facts; qualified Open? stays display-only. Attached
uses fresh positive tmux client counts, including clients elsewhere. Empty and
unknown views remain accessible, with an inspection hint to All or Local.

Finite owner and viewer jobs have independent request/run locks, markers,
deadlines, and retry budgets. Viewer scans never inventory SSH peers. Successful
owner results publish individually; per-host epochs reject older results after
newer reconciliation. Owner and viewer clocks stay separate. Changed supporting
owner facts invalidate cached viewer matches and can renew a completed scan
immediately; failed scans retain their cooldown. Future clocks cannot hold a
running marker indefinitely.

Rofi 2.0 reads keep-filter and keep-selection from the preceding frame. Every
frame now arms those flags, and explicit new-selection controls reset versus
preservation. This fixes filter loss on the first view change and selection
loss on the first timer redraw. The first prepared frame is claimed once; later
mode re-entry performs normal preparation. Upstream behavior is visible in
[Rofi 2.0 script mode](https://github.com/davatorium/rofi/blob/2.0.0/source/modes/script.c#L315-L383).

## Acceptance

| Boundary | Evidence | Result |
| --- | --- | --- |
| Source | `scripts/check`: 239 tests, compile, fixtures, manifests, Ruff, shell checks, executable and text checks | Passed |
| Producer bundle | Tmux Host Mesh sync against SSH Plus | Passed; unchanged `f3e62a5d195e9359a35f832010f3a016d8038b6b` |
| Consumer bundles | Agent Plus sync against both producers | Passed; Tmux bundle unchanged through baseline `5b02f84` |
| Wheel | Built wheel installed in private `/tmp` environments on both endpoints | All Python module bytes match candidate; both console scripts exist |
| Installed public CLI | Live local inventory and opt-in viewer inventory on Snap and Starship | Schema-valid single JSON documents, empty stderr, correct Mesh revision |
| Installed private jobs | Owner and viewer refresh from each endpoint | Complete; both logical hosts present and healthy |
| Native Rofi on Snap | Isolated fixture callbacks through real launcher and managed key bindings | Seven checks passed |
| Native installed Rofi | Real inventory, Open/Attached navigation, native Escape | Passed; no lifecycle actions |
| Managed preparation | Launcher patch check and isolated chezmoi symlink dry-run | Passed; ordinary source/targets unchanged |

The seven native fixture checks cover initial bookmarked Enter, arrow-only
selection followed by Escape, remembered view after cancel, filter preservation
across Open/Attached, action cycling and Ctrl+G, first/subsequent timer redraws,
and initial Cancel selection in kill confirmation. Fixture lifecycle calls are
recorded stubs; real installed GUI checks perform observation only. Source
lifecycle tests exercise disposable tmux fixtures and identity guards.

A controlled fast/slow peer regression proves publication and visibility of the
healthy peer while the other is still running. Other regressions cover late
results after mutation reconciliation, expired owner facts with a fresh viewer
scan, changed owner inputs, unsafe preference/timing files, spawn-gap request
deduplication, scan-only renewal, and clock rollback. This proves script-model
visibility; it is not a measured multi-peer graphical transport latency.

## Timing

The baseline was collected before changing the refresh code. Each endpoint has
20 warm frames and five cold/refresh samples. Timings are monotonic, headless,
and use isolated cache/state roots. Counts, samples, observed p95, maxima, and
phase events are in [evidence](evidence/2026-10-06-picker/). Candidate timing
samples precede final launcher one-shot hardening and unavailable-row label
precedence. Those changes do not alter the healthy timing path and have
separate source/native/installed checks. Session populations can change during
live-host measurement; these are descriptive samples, not a controlled quiet-host
benchmark. The exact observed row counts are included in the reports.

| Measurement, milliseconds | Snap baseline | Snap candidate | Starship baseline | Starship candidate |
| --- | ---: | ---: | ---: | ---: |
| Warm prepared frame, median | 102.3 | 100.6 | 68.7 | 65.9
| Warm prepared frame, observed p95 | 112.9 | 110.1 | 74.5 | 68.0
| Cold prepared frame, median | 98.8 | 98.6 | 69.2 | 63.7
| Combined old refresh / serial new jobs, median | 767.8 | 826.9 | 661.5 | 617.3
| New owner job, median | — | 630.5 | — | 496.1
| New viewer job, median | — | 198.9 | — | 119.2

Use the linked JSON values for exact candidate samples; the table rounds to one
decimal. Warm opening did not materially change. Serial split work can cost
more because independent viewer jobs perform their own context/revision
checks. The gain is reduced coupling and earlier availability of completed
host rows, rather than a universal speedup of a forced full refresh.

A separate 32-second, one-callback-per-second idle experiment observed two
baseline combined helpers on each endpoint. The candidate requested one owner
helper and three local viewer helpers; its instrumentation observed exactly one
SSH attempt on each endpoint. Baseline SSH/byte counts are unavailable because
that release lacks timing hooks, so those fields are null, not inferred zeros.
The idle run preceded final input-change/clock guards, which preserve the same
healthy renewal policy. Native timer/filter behavior was validated separately.

Snap's typical Host Mesh list cost is about 98 ms; local tmux collection is
about 2 ms and cached rendering about 1–2 ms. The normal remote command costs
roughly 300 ms, but that includes transport and remote collection; these samples
do not isolate SSH handshake time. Effective SSH configuration reports
ControlMaster false / ControlPersist no for both peer perspectives. No SSH
configuration was changed. Snap's real installed dialog surfaced in roughly
220–230 ms in brief single samples; this is a smoke measurement, not p95.

Idle cached ticks generally cost only a few milliseconds. Renewal still pays
for Host Mesh and local reads. Native push/watch remains a subsequent pass:
these changes establish the exact identity, independent clocks, progressive
publication, and rejection of late results that a watch would require.

## Reproduction and release

Run source checks and each producer/consumer sync gate from its own checkout.
For passive timing with isolated state:

```sh
./scripts/measure-refresh --output /tmp/tmux-plus-profile.json --idle-seconds 32
```

For opt-in diagnostics, set `TMUX_PLUS_TIMING_FILE` to a file inside an owned
private directory. Events are bounded and contain durations, host IDs, byte
counts, and exit status; they do not contain command arguments or output
contents. Public JSON and Rofi protocol output remain unchanged.

Build/install the candidate wheel into an isolated environment, then use its
`rofi-tmux-plus-rofi` launcher with the managed script mode and key bindings.
The isolated candidate environments and local GUI artifacts are recorded in
the evidence reports; they are temporary test installations.

The prepared [managed launcher patch](evidence/2026-10-06-picker/managed-launcher.patch)
adds the wrapper link and changes Mod+G. It passes `git apply --check
--unidiff-zero` against the current chezmoi checkout and an isolated symlink
apply dry-run. Apply it only together with a published candidate/release archive
pin and checksum, because the ordinary 0.5.1 tree has no wrapper. Update the
managed interaction guidance and status ledger around that final exact tuple,
then run the clean-checkout candidate gate, scoped chezmoi dry-run/apply, and
installed fleet gate. Keep current Agent Plus / Observer pilot selections.

The exact managed tuple/deployment gates are deliberately not claimed here:
source is an uncommitted candidate, no archive for it is published, and managed
pins still select the released baseline. Starship GUI acceptance was not run;
Snap was the user-selected graphical endpoint. Further personal workflow and
visual acceptance remains an operator review of this candidate.
