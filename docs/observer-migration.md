# Observer client migration

Candidate: Tmux Plus `0.7.0a1`. This is an implementation checkpoint, not a
selected release or completion of the native/GUI/managed gates.

## T13 fresh inventory checkpoint

The default public `inventory` path delegates to Observer's supported
`DirectInventory` client. Host selection, panes/options and fresh access remain
explicit. Failed host rows expose no historical sessions. Optional local viewer
enrichment uses the same captured catalog; lifecycle actions continue to perform
their independent native validation. Explicit legacy read adapters remain only
for the existing private refresh harness until the picker migration is accepted.

The dependency version alone is insufficient to identify a prerelease build.
`observer-artifact.json` pins the accepted producer source
`3beab8a94719c9690914fc73ca15697e81025e2e`, wheel SHA256
`b511fb12304b7693879f7fb878ddacd1c32f1afa6e4ccf2738d34405f24a4b84`,
and all 35 Python modules. Imports validate exact runtime coverage and bytes;
absence or drift fails visibly without a native collection fallback. A managed
frozen-wheel installation may expose the fixed
`~/.local/share/tmux-observer/python` root. Distribution/installed artifact
validation must additionally verify the full wheel and managed launcher bytes.

Source checks: 246 tests, canonical bundle checks, compile, Ruff, ShellCheck and
candidate text checks passed using Snap's native CPython 3.14.7 with the exact
frozen Observer wheel installed in an isolated environment. The initial uv
CPython 3.13.7 environment lacked `os.pidfd_open`; four existing viewer tests
errored and one failed there. No viewer behavior or assertion was weakened.

Still required before closing T13: installed frontend native compatibility
checks against the unchanged v1 contract, including absent/broken Mesh and
local-only hostname aliases. Review found that Observer's fallback currently
accepts only the short hostname, whereas the released frontend also accepts its
FQDN. Confirm and repair this at the producer's direct client boundary before
claiming complete compatibility; do not silently normalize requests in Rofi.

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
it; a dead/stalled/malformed watch revokes cached positives. Eight focused tests
include actual child pipes and a simulated 30-second callback gap.

The first-party Mode ABI 7 wrapper delegates to its fixed packaged helper. It
reads only bounded, owned regular notifications, wakes callback 28, checks local
BOOTTIME expiry/liveness, coalesces callbacks, reacquires borrowed state after
updates, and removes its timer/root on destruction. The launcher verifies the
Rofi binary and source/helper/library hashes before loading it. There is no live
compilation or script/native-read fallback. Source checks now pass 271 tests.
Three isolated compilations with `-Wall -Wextra -Werror` yield library SHA256
`99e1e241aaf88bbfa53f10a975b023788f65eedcf8c255202f98fc01b42c9b41`.
These were compile/tuple checks only; no production mode was loaded.

Build the mode into a frozen candidate package before its wheel:

```sh
./scripts/build-native-mode --output-directory rofi_tmux_plus/native
uv build --wheel --out-dir /path/to/owned/candidate
```

The native descriptor stays `built_unaccepted`; acceptance evidence is separate.
The wheel is platform-specific and independent of the Python extension ABI.
Generated library/descriptor files are ignored in the source checkout. Missing,
changed or unsupported integration files cause bounded startup failure.

Still pending: installed fresh CLI compatibility, the packaged wheel/helper
permission check, production native GUI/timing/failure/lifetime acceptance,
including cancellation and mode re-entry, and managed selection. Source tests
and compilation do not establish automatic native rendering. Saved context,
complete references and pending action intent remain frontend state. Snap GUI
acceptance and Starship GUI status are separate.

T15 publishes accepted exact artifacts and selects them with scoped chezmoi
rollout, recovery and rollback checks on Snap and Starship. The producer's
accepted always-on scope makes physical suspend optional. Both hosts remain
awake throughout this implementation and its simulated recovery checks.
