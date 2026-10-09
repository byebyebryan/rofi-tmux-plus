# Fleet performance consumer acceptance

2026-10-09. Tmux Plus 0.10.0a1 changes only its package version and exact
Observer pin. Picker state, actions, qualification, watches and seven producer
contracts remain unchanged. The producer implements checked admission, cached
byte accounting, material/receipt separation, unchanged Mesh topology retention
and three-second proofs for unchanged remote facts. Fresh remote C3 scans remain
independent; no retained remote positives or absence are invented.

Frozen consumer runtime `69209e374fb30bfeb85ab135b092b104d20ad4f0` builds twice
to wheel SHA-256
`58572788a6ec476b9181fced1a85a32671ecedb39e90d5859c264692efeacac3`.
Its pin checks all 78 Observer modules from runtime
`ad6073953c3e1f4237950fffc53b8e0a111120fa`, wheel SHA-256
`efb1e7b72e0128bf45c10803d366304f7b108aec5ac5aaf2fffafa4efb23d880`.
The managed bundle has 287 files plus its manifest, SHA-256
`862d2fff3792d84dfd1d556663d1d7c69f3ecba4f3004c0ad3d5287788764cb5`.
Later documentation commits do not replace these accepted runtime artifacts.

The independent source gate passes 293 tests and contract/style checks. The
[installed frozen picker](evidence/2026-10-09-fleet-performance/picker-snap.json)
passes all 23 Snap cases: prepared startup, cycling after the original lease
expires, retained local Open, native attach/detach updates, explicit terminal
refresh tickets, typing/selection, native keys mode/cancel, confirmation
preservation and reader loss/replacement. No ordinary preferences or sessions
were changed; owned processes/runtime were removed and previous focus restored.
The [renewed-view screenshot](evidence/2026-10-09-fleet-performance/tmux-fleet-fp3-picker-snap-renewed-views.png)
shows current normal rows after cycling beyond the original lease.

Producer native/resource gates and scoped paired managed rollout are separate.
The producer design and remote-retention disposition are documented in
[Tmux Observer](https://github.com/byebyebryan/tmux-observer/blob/main/docs/fleet-performance-design.md).
Physical suspend remains optional under the agreed always-on scope. Starship
receives no foreground input in this pass.
