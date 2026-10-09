# CPU performance consumer

2026-10-09. Tmux Plus 0.11.0a1 pins the CPU performance candidate Observer
0.5.0a1. Picker behavior, preference state, watches, action guards and the native
mode remain unchanged. The producer groups structural traversal, combines fresh
decoding with immediate semantic admission and reuses plain session references.
It keeps all seven wire contracts and native/freshness timing unchanged.

The exact producer is `42e9ebf2121280fd00b135e855cf2569f9791c94`, wheel SHA-256
`ae5cb6ec2310a0eb6901f39d5c405b4858d717f07883e024628cdd80587eab9e`.
The consumer pin checks the complete 78-module runtime. The independent source
gate passes all 293 tests and contract/style checks against that installed wheel.
Native producer/resources/capacity acceptance now passes independently. Managed
rollout remains a separate gate.

The first producer freeze had matching wheel metadata but a stale Python version
constant. The consumer exact-pin guard refused it. The corrected producer and
repeated immutable builds supersede that candidate, and the producer source gate
now checks version parity. Earlier evidence is retained with its original scope.

Producer decisions, measured pipeline limits and remaining gates are captured in
[the CPU design](https://github.com/byebyebryan/tmux-observer/blob/main/docs/cpu-performance-design.md).
Source acceptance alone does not establish ordinary CPU savings or deployment.
Only Snap receives foreground tests; physical suspend remains optional.

The frozen frontend is `cd4db5ce0ff9d6fe09dfa393d88a3479adc8a09a`, wheel SHA-256
`9382ebb3a6f107e49e565b90226dd91b017b7d0508d2dd1568ff43cef4837368`.
Repeated wheels and managed bundles match. The bundle has 321 files plus its
manifest, SHA-256
`cfda29d759fc6009c4dd8b9d9632069882fda7bae6099797faa44d2581027455`.
The [exact-wheel picker](evidence/2026-10-09-cpu-performance/picker-snap.json)
passes all 23 Snap cases, including view cycling beyond the original lease,
native attach/detach, retained qualified Open, completed refresh notice,
typing/selection, confirmation, reader replacement and owned cleanup.
The [renewed-view screenshot](evidence/2026-10-09-cpu-performance/tmux-cpu-cp3-picker-snap-renewed-views.png)
and [completed refresh](evidence/2026-10-09-cpu-performance/tmux-cpu-cp3-picker-snap-refresh-complete.png)
have been visually checked. These gates preserve ordinary sessions/preferences
and provide no Starship foreground or physical-suspend acceptance.

The [managed picker](evidence/2026-10-09-cpu-performance/managed-picker-snap.json)
also passes all 23 Snap cases using the exact installed bundle and actual Mod+G
arguments. Its [renewed views](evidence/2026-10-09-cpu-performance/tmux-cpu-cp4-managed-picker-snap-renewed-views.png)
and [completed refresh](evidence/2026-10-09-cpu-performance/tmux-cpu-cp4-managed-picker-snap-refresh-complete.png)
screenshots have been visually checked. Both endpoints pass exact installed
bytes, serial recovery, actual matching rollback/reselection and disposable
CLI lifecycle gates. Native/prepared/fresh references agree for nine Snap and
ten Starship sessions. Provider selection and source reconciliation have their
separate [managed record](https://github.com/byebyebryan/dotfiles/blob/main/docs/tmux-observer-cpu-performance-operations.md).
