# Tmux Plus 0.6.0 release capture

The [release report](release.json) records published source and managed
revisions, the archive checksum, gates, deployment scope, and acceptance
limits. Installed reports compare all 172 tracked files and executable modes,
links, and binding bytes; ordinary preferences and Agent/Observer selections
are preserved. Live reports cover both configured host perspectives and
disposable exact-reference lifecycle cleanup. Native Snap evidence uses actual
installed binding arguments and performs no lifecycle actions. Starship GUI
and operator visual/focus acceptance remain pending.

The selected Observer/Plus artifact gate fails `provider_fingerprint` on both
hosts because installed provider binaries differ from the pilot manifest.
This rollout does not change provider installations. The stable released tuple
gate, complete managed checks, and scoped Tmux installed checks pass separately.

Starship's original dirty checkout is retained. Deployment first uses the
published clean checkout, then merges only four Tmux source inputs into the
ordinary source so future scoped applies retain the release. The installed
Starship report checks that reversing these inputs in memory reconstructs
the original content digest and unrelated working status.

The reproduction helpers expect the release manifest and before snapshots in
`/tmp`. For a fresh installed-file comparison, copy `release-manifest.json` to
`/tmp/tmux-plus-release-manifest.json`, then run `verify-installed.py before`
and `verify-installed.py verify`, passing `--host snap` or `starship` and
`--source /home/bryan/.local/share/chezmoi`. Worktree-preservation options are
for the original recorded rollout only.

`check-tmux-live.py --host HOST --source SOURCE` uses the managed gate's public
CLI checks and creates, renames, and kills only disposable gate-owned sessions
on both configured hosts. It also checks installed independent owner/viewer
jobs and guards ordinary SSH history. Run each host perspective sequentially.
The helpers use private cache/preferences and perform no provider or window
actions. They are capture tools, separate from deployed product code.
