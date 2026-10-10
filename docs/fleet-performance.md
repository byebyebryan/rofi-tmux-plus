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

## Corrected final release and managed acceptance

The final selected frontend is **0.10.0a2**. The a1 tag froze the old CI wheel
filename although its later main-branch review fixed that filename; its tag CI
failed the download step. Preserve that immutable release and select a2,
runtime `1b45adad7488bc077b01b7e94a61fc2b2265abb4`, which contains the correction.
All packaged frontend Python, native mode and Observer-pin bytes are identical
between a1 and a2. The package version and release workflow selection differ.
The unchanged producer resource gates therefore still apply to Observer's
exact accepted 0.4.0a1 wheel.

The a2 wheel builds twice with SHA-256
`d69ae6c8244eb586c28214307d78be3ae77cd8824eda06c1851c770322cc47da`.
Its managed bundle has 299 files plus the manifest, SHA-256
`fb96e25a1eed9e2a3d8c03a44902788bcb7299f8c43faedeef4ea1c3928de3d4`.
Published downloads match the accepted artifacts. The source gate again passes
293 tests; [main CI](https://github.com/byebyebryan/rofi-tmux-plus/actions/runs/37900339676)
and [a2 tag CI](https://github.com/byebyebryan/rofi-tmux-plus/actions/runs/37900557585)
pass. Both the [installed wheel](evidence/2026-10-09-fleet-performance/picker-a2-snap.json)
and [managed Mod+G path](evidence/2026-10-09-fleet-performance/managed-picker-a2-snap.json)
pass all 23 Snap graphical cases, with isolated preferences and owned fixtures.

Both hosts select Observer 0.4.0a1 / Plus 0.10.0a2 through scoped chezmoi.
Installed-byte, owner/reader restart, paired rollback/reselection and full
reference action checks pass. Final native/fresh/prepared parity is nine Snap
and ten Starship sessions from either endpoint. Ordinary references,
generations, hooks and unrelated Agent/Kitty controls are preserved. The
[managed operations record](https://github.com/byebyebryan/tmux-observer/blob/main/docs/fleet-performance-design.md)
retains the deployment evidence separately.
