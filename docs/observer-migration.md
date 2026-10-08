# Observer client migration

Candidate: Tmux Plus `0.7.0a1`. T13 installed fresh CLI compatibility is accepted;
the native GUI and managed gates remain open.

## T13 fresh inventory checkpoint

The default public `inventory` path delegates to Observer's supported
`DirectInventory` client. Host selection, panes/options and fresh access remain
explicit. Failed host rows expose no historical sessions. Optional local viewer
enrichment uses the same captured catalog; lifecycle actions continue to perform
their independent native validation. Explicit legacy read adapters remain only
for the existing private refresh harness until the picker migration is accepted.

The dependency version alone is insufficient to identify a prerelease build.
`observer-artifact.json` pins the accepted producer source
`d5a2e97a4e5c2a65599d45818680635f5c790c99`, wheel SHA256
`06c5b77e36901d0de5d62cf2041edf1733ce34c346a6aa574a323bb50889fb0d`,
and all 35 Python modules. Imports validate exact runtime coverage and bytes;
absence or drift fails visibly without a native collection fallback. A managed
frozen-wheel installation may expose the fixed
`~/.local/share/tmux-observer/python` root. Distribution/installed artifact
validation must additionally verify the full wheel and managed launcher bytes.

Source checks: 276 tests, canonical bundle checks, compile, Ruff, ShellCheck and
candidate text checks passed using Snap's native CPython 3.14.7 with the exact
frozen Observer wheel installed in an isolated environment. The initial uv
CPython 3.13.7 environment lacked `os.pidfd_open`; four existing viewer tests
errored and one failed there. No viewer behavior or assertion was weakened.

T13 installed frontend compatibility passed 30 native cases across Snap and
Starship against the unchanged v1 contract, including absent/broken Mesh,
fresh rename/full references, panes/options, local-only hostname aliases,
optional viewers and passivity. Typed frontend failures retain exit status 2.
The producer's bounded FQDN repair passed 28 native
installed cases on Snap and Starship and four installed recovery simulations.
Only its direct-client module changed; the other 63 payloads match the previously
accepted G3 artifact. See Observer's `native-direct-acceptance.md` and committed
FQDN evidence. Those checks are producer acceptance, not installed frontend proof.
The facade also retains well-formed future Host Mesh error codes.

The frozen frontend source is `bb35d947653a842355ce8f164ecf2301355ec80e`;
its platform wheel SHA256 is
`c0ebe97e596cd5e176be647ce9c4eed719e358e385df7ecd7cae3e8693a84723`.
Repeat builds were byte-identical. All 38 installed payloads, including license,
were verified on both hosts; the installed helper retains executable permissions.
Snap additionally passed the binary/library tuple and core-pin checks before
mode loading. Evidence is in [the installed client record](evidence/2026-10-08-observer-client/installed-package.json)
and [native facade cases](evidence/2026-10-08-observer-client/native-facade.json).
All disposable endpoint units, children, sockets and roots were removed.

## Next gates

The T14 prepared-model checkpoint replaces the default picker observation jobs
with one public cached read. Missing/warming readers fail visibly; startup and
callback 28 admit no refresh, create no service and collect no native facts.
The launcher supplies a private per-picker runtime and captured desktop context.
Alt+R stores a publisher/ticket identity there; each pending callback uses scoped
terminal lookup even when rows/revision are equal. A deadline or lost reader ends
the matching notice without retrying a source. Post-action reconciliation requests
only the affected owner; actions keep their independent validation.

Owner and desktop expiry use local BOOTTIME. Cached navigation rechecks boot,
namespace, context and expiry, preserving historical complete references while
revoking Open/Attached positives. Wall-clock activity labels remain separate.
Seventeen focused tests cover these composed behaviors, fixed notice lifetime,
pending-target retention, and bounded regular runtime files. The existing timeout
tests now explicitly require read-only adoption rather than refreshing jobs.

The next source checkpoint adds one owned public-client watch per picker, with
bounded framing/liveness, ordering/context checks, one latest notification,
read-only reconnect and owned child cleanup. Quiet lease renewals update expiry
metadata without waking the UI. Material changes, resync and ticket results wake
it; a dead/stalled/malformed watch revokes cached positives. Ten focused tests
include actual child pipes, a simulated 30-second callback gap and launcher
SIGKILL cleanup. A Linux parent-death guard terminates the owned public watch
if its launcher disappears, including the startup parent replacement race.
The first production-mode smoke run found that SIGTERM of the launcher could
leave its owned Rofi process alive. The launcher now also guards the fixed Rofi
exec against parent death and handles TERM/HUP through normal context cleanup.
The guarded-exec crash test confirms termination and reaping across exec.
That repair reopens the frozen frontend GUI/package checkpoint; the accepted
fresh facade above remains evidence for its recorded artifact only.

The first-party Mode ABI 7 wrapper delegates to its fixed packaged helper. It
reads only bounded, owned regular notifications, wakes callback 28, checks local
BOOTTIME expiry/liveness, coalesces callbacks, reacquires borrowed state after
updates, and removes its timer/root on destruction. The launcher verifies the
Rofi binary and source/helper/library hashes before loading it. There is no live
compilation or script/native-read fallback. Startup failures use a bounded public
Rofi error display without loading the candidate mode. Source checks pass 276 tests.
Three isolated compilations with `-Wall -Wextra -Werror` yield library SHA256
`99e1e241aaf88bbfa53f10a975b023788f65eedcf8c255202f98fc01b42c9b41`.
These were compile/tuple checks only; no production mode was loaded.

Build a clean committed candidate with the pinned native mode and hashed backend:

```sh
./scripts/candidate-artifact build --revision HEAD --output /path/to/new/candidate
./scripts/candidate-artifact verify /path/to/new/candidate/candidate.json
```

The builder owns and removes a detached temporary worktree, compares every
packaged payload with that source, and checks the helper's executable bit.
It records the wheel, producer pin and native tuple in one immutable descriptor.
The native descriptor stays `built_unaccepted`; acceptance evidence is separate.
The wheel is platform-specific and independent of the Python extension ABI.
Generated library/descriptor files are ignored in the source checkout. Missing,
changed or unsupported integration files cause bounded startup failure.

Still pending: production native GUI/timing/failure/lifetime acceptance,
including cancellation and mode re-entry, and managed selection. Source tests
and compilation do not establish automatic native rendering. Saved context,
complete references and pending action intent remain frontend state. Snap GUI
acceptance and Starship GUI status are separate.

T15 publishes accepted exact artifacts and selects them with scoped chezmoi
rollout, recovery and rollback checks on Snap and Starship. The producer's
accepted always-on scope makes physical suspend optional. Both hosts remain
awake throughout this implementation and its simulated recovery checks.
