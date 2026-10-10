# Mesh-backed Observer candidate

Source candidate Tmux Plus 0.12.0a1 pins Observer 0.6.0a1 and its complete runtime
manifest. The frontend retains Fleet v1, Tmux Session v1, desktop freshness and
independent action guards. Observer's new Mesh backend is selected by the fleet
service; importing the prepared frontend requires no Mesh connection or collector.

The producer's [integration ledger](https://github.com/byebyebryan/tmux-observer/blob/main/docs/mesh-integration.md)
owns cached-state transport, source receipts, explicit refresh compatibility and
the separately reviewed Mesh dependency. The Mesh a4 candidate is frozen from
`2cc12accb331db7ee65ab426da09576a907b291c`, matching the separately selected
Agent Mesh release. Older Observer/Plus selection remains independent until a
paired Tmux rollout is accepted. Shared launcher/source changes must preserve
and verify both domains.

`scripts/accept-native-picker --transport mesh` uses exact installed wheels,
disposable native sessions and the real Mesh backend. Its fixed local-only
catalog is a fixture shared by the consumer and Mesh authority APIs. It exercises
local binding/viewer freshness, refresh, filtering, native watch recovery and
reader restart. It establishes no remote desktop, ordinary resource, managed
selection, Starship foreground or physical sleep acceptance. The legacy backend
remains selectable for independent comparison and rollback.

Candidate source, package and Snap graphical evidence will be recorded here
after each gate. No source pin alone authorizes artifact promotion.
