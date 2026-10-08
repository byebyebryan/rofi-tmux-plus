# Observer client migration

Tmux Plus `0.7.0a1`: T13 fresh CLI compatibility and T14 prepared/native picker
acceptance and T15 managed selection pass for the recorded tuple below.
The user selected Starship for graphical tests while Snap is in active use.
No host was suspended; physical sleep/wake is optional in the always-on scope.

## Accepted tuple

| Component | Exact identity |
| --- | --- |
| Observer source | `d5a2e97a4e5c2a65599d45818680635f5c790c99` |
| Observer wheel SHA256 | `06c5b77e36901d0de5d62cf2041edf1733ce34c346a6aa574a323bb50889fb0d` |
| Frontend source | `92b5c357a009822413d229b4cd4fe18e1a777417` |
| Frontend wheel SHA256 | `4d28a8a103025fc467f4f558a4071f2573fd628170dfa726442f89685811eaa0` |
| Native library SHA256 | `77c7402faed64e1175afc13b3134fd657a93611c0273d2182f1fc0c6fca793d8` |
| Rofi binary SHA256 | `4f8dcd3a87d4c41af3e7c5f3ab4891a0926f21ab659072e5471e7332cf30a1ca` |

The first-party Mode ABI 7 library targets the recorded Rofi 2.0.0-dirty x86_64
binary/header tuple. The launcher validates files and the binary before loading;
missing/drifted/unsupported integration produces a bounded public error display.
There is no live compilation or native-collection fallback.
Two committed-source builds produced identical wheel bytes. All 39 installed
runtime/license payloads and executable helper permissions were verified on both
hosts through [the installed facade](evidence/2026-10-08-native-picker/native-facade.json).
The immutable [candidate descriptor](evidence/2026-10-08-native-picker/frontend-candidate.json)
retains `built_unaccepted`; acceptance is this separate scoped record.

The producer's FQDN repair passed 28 native direct cases and four installed
recovery simulations. Only its direct-client module changed; the other 63
runtime/data/license payloads match the previously accepted always-on G3 wheel.
See Observer's `native-direct-acceptance.md`, parity and resource records.

## Behavior and verification

The default public `inventory` delegates to the pinned Observer `DirectInventory`.
Host selection, fresh reads, panes/options and legacy v1 JSON remain explicit.
Non-ok fresh rows contain no historical sessions. Optional viewer enrichment uses
one captured catalog; lifecycle actions retain their independent native validation.
The unchanged canonical facade passes 30 installed/native cases on Snap/Starship,
including missing/broken Mesh, aliases, rename/full references, typed exit status 2,
optional viewers and roster/hooks/options/geometry passivity.

The picker uses one public cached read and one owned public-client watch.
Startup and callback 28 admit no refresh, activate no service and collect no
native facts. Missing/warming readers fail visibly. Alt+R retains one scoped
publisher/ticket identity; terminal lookup clears its notice even with equal rows.
Post-action reconciliation requests only the affected owner. Owner and desktop
leases use local BOOTTIME, and each cached navigation/render rechecks scope,
watch health and expiry. Complete references survive expiry while Open/Attached
positives are revoked; those views supply no action authority.

The private watch has bounded framing/partial-input/silence/stderr limits,
read-only reconnect, latest-notification coalescing and owned process cleanup.
Quiet lease renewals extend metadata without waking the UI. Material changes,
ticket results, recovery and expiry wake callback 28. The native mode reacquires
borrowed state after synchronous updates and removes its timer/root on destruction.
Launcher TERM/HUP and parent death stop owned Rofi/watch children.

Source checks pass 278 tests, compile, canonical bundle checks, Ruff, ShellCheck
and candidate text checks on native CPython 3.14.7. The existing short-deadline
holding-wrapper test failed once, then passed individually and in the full gate;
its assertion and production timeout were unchanged. An earlier uv CPython 3.13
lacked `os.pidfd_open`; native Python was used without weakening viewer checks.

[Starship production acceptance](evidence/2026-10-08-native-picker/starship-picker-accepted.json)
passes 20 cases with the exact installed tuple, actual owner/fleet processes,
disposable native sessions and isolated preferences/runtime. It verifies typing,
filter/caret, complete selection, view arrows, remembered empty Open view,
frozen pending confirmation/cancel, actual mode reentry plus subsequent adoption,
stalled watch/expiry/malformed notification and recovery, lost/new reader,
absent-reader display, native cancellation and launcher cleanup. Inspected native
captures show the changed UI and explicit absent-reader outcome. Ordinary sessions
were untouched; owned roster/attachments/windows remained unchanged apart from
the harness's named renames. No SSH was started by the local-only picker fixture.
DMS idle inhibition was temporarily enabled on Starship and restored to its prior
state after every run. No Snap graphical input followed the user's restriction.

## Timing and fixes

100 uninstrumented prepared frames on Starship have p95 **73.36 ms**, below the
150 ms frame target, with no foreground tmux/SSH collection. Three native launches
observed surfaces at 134-155 ms and first native callback completion at 313-324 ms.
These are separate individual checkpoints, not a graphical p95 or a precise
compositor presentation timestamp. The read-only recorder adds callback logging;
it is excluded from warm-frame samples. Native screenshots establish appearance.

The final explicit refresh took 585 ms from request callback start to notice-free
callback completion. That completion was 9 ms after an independent scoped ticket
probe observed service completion; probe polling/CLI overhead limits precision.
An earlier fixture sample took 1.57 seconds with the same producer, showing source
scheduling can vary. These samples do not establish remote/fleet refresh p95.

Native testing repaired launcher termination, empty-view hotkey suppression,
recovered observation notices resurfacing on page changes, and first-invocation
error serialization. The [rejected earlier native run](evidence/2026-10-08-native-picker/initial-error-rejected.json)
retains its failed absent-reader result; it is superseded only by the corrected
artifact's separate acceptance. Empty views keep custom-input dispatch inert.
Initial failures now use LF headers and declare the continuation delimiter, so
Rofi parses the visible error and subsequent read-only callbacks correctly.

## Publication and managed acceptance

`candidate-artifact build` freezes committed source with the pinned native mode
and constrained backend. `release-bundle` combines that source archive with the
verified platform wheel, preserves executable bits and writes a complete member
manifest for scoped managed verification. It adds no acceptance by assembly.

Observer is published at `v0.1.0a1`; its downloaded wheel checksum matches the
accepted input, and source CI passed at `28a74b6`. Frontend `v0.7.0a1` points to
release/review commit `9c39575`, while its runtime artifacts retain source `92b5c35`.
Its wheel checksum matches the accepted input. The deterministic managed source/
native bundle SHA256 is
`1a5701c0887aa2304e97f6fc4ab0c7512a96a919dec17e899102fdac48ce5e46`.
Frontend CI passed at `9c39575` after installing the exact pinned Observer wheel.

T15 is accepted through [the managed operations record](https://github.com/byebyebryan/dotfiles/blob/main/docs/tmux-observer-operations.md)
and its exact tuple/member evidence. Scoped chezmoi installation on Snap and
Starship verifies all 69 Core members, 198 frontend members, manifest, launchers,
units/drop-ins, owner enablement and desktop startup. Both final prepared frames
have ready local/remote owners, Mesh and desktop observation; the fresh v1 facade
and headless picker frame pass. Owner/desktop restart creates new publisher and
reader incarnations. Rollback restores every previous control and frontend byte,
stops the scoped units and leaves a healthy old facade; reselection passes again.
Preexisting native session references survive throughout.

The installed managed bundle also passes 20 graphical cases on Starship using
the rendered Mod+G arguments and desktop theme with isolated preferences/runtime
and disposable native sessions. Diagnostic key/mode/window additions are recorded
separately from the managed binding. Snap receives headless deployment/acceptance
only. Both hosts stay awake; no alarm or suspend is attempted. Lifecycle extraction
and native event experiments remain separate follow-ups.

The managed-bundle run measured warm frame p95 71.38 ms, three observed surfaces
at 132–157 ms, and one explicit local-fixture refresh clearing its notice at
738 ms, 28 ms after the independent terminal-ticket probe. These remain separate
descriptive frontend/service checkpoints, not remote networking or graphical p95.
The exact released cross-tool gate also passes 74 SSH, 278 Tmux and 320 Agent
tests (four existing Agent skips), contract syncs and isolated public PATH probes.
