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

T14 replaces picker observation jobs with the public cached facade, BOOTTIME
expiry and explicit refresh tickets. The launcher then owns one watch per picker
and a verified native notification mode. Background adoption remains read-only;
saved context, complete references and pending action intent remain frontend
state. Snap GUI acceptance and Starship GUI status are separate.

T15 publishes accepted exact artifacts and selects them with scoped chezmoi
rollout, recovery and rollback checks on Snap and Starship. The producer's
accepted always-on scope makes physical suspend optional. Both hosts remain
awake throughout this implementation and its simulated recovery checks.
