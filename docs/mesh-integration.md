# Mesh-backed Observer candidate

Source candidate Tmux Plus 0.12.0a1 pins Observer 0.6.0a1 and its complete runtime
manifest. The frontend retains Fleet v1, Tmux Session v1, desktop freshness and
independent action guards. Observer's new Mesh backend is selected by the fleet
service; importing the prepared frontend requires no Mesh connection or collector.

The producer's [integration ledger](https://github.com/byebyebryan/tmux-observer/blob/main/docs/mesh-integration.md)
owns cached-state transport, source receipts, explicit refresh compatibility and
the separately reviewed Mesh dependency. The Mesh a2 candidate is frozen from
`fd40916f2b0e5c9d2a3f8b51d0da02585894ddcb`; the deployed authority a1 and older
Observer/Plus selection remain independent until a paired rollout is accepted.

`scripts/accept-native-picker --transport mesh` uses exact installed wheels,
disposable native sessions and the real Mesh backend. Its fixed local-only
catalog is a fixture shared by the consumer and Mesh authority APIs. It exercises
local binding/viewer freshness, refresh, filtering, native watch recovery and
reader restart. It establishes no remote desktop, ordinary resource, managed
selection, Starship foreground or physical sleep acceptance. The legacy backend
remains selectable for independent comparison and rollback.

Candidate source, package and Snap graphical evidence will be recorded here
after each gate. No source pin alone authorizes artifact promotion.
