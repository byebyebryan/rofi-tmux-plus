# Tmux Plus 0.6.0 release review

Date: 2026-10-07. This release promotes the remembered-context, Open/Attached,
and independent-refresh pass described in the
[implementation plan](tmux-plus-picker-refinement-plan.md).

Review found a private-cache validation gap: `lastAttemptAt: null` was accepted
and could break refresh arithmetic. The release rejects null, boolean, string,
and negative retry timestamps before loading those rows. A regression covers
all four cases while preserving the intentionally nullable successful-owner
timestamp. The final source gate passes 240 tests, compilation, contract and
fixture checks, Ruff, shell checks, and executable/text checks.

The public Host Mesh v1 and Tmux Session v1 bundles remain unchanged. Open and
Attached only filter observed sessions; they add no provider semantics or
close authority. Refresh uses finite independent jobs and progressive owner
publication. Persistent push/watch is a future pass.

The [candidate evidence](tmux-plus-picker-validation.md) retains its original
`0.6.0a1` version, sample counts, private wheel identity, and Snap graphical
acceptance. Those measurements are historical samples rather than release
deployment evidence. Starship graphical acceptance remains unrun.

Managed rollout uses an immutable source archive and its SHA-256 together,
the new `rofi-tmux-plus-rofi` link, and the existing Mod+G arguments through
that launcher. Only Tmux files and the shared binding file are deployment
targets. Agent Plus and Observer pilot selections are preserved. Starship's
dirty working checkout is preserved by using a separate published chezmoi
checkout for scoped application.

Exact source, managed, installed, CI, and desktop results are recorded in the
[current deployment ledger](https://github.com/byebyebryan/dotfiles/blob/main/docs/rofi-plus-status.md).
The previous archive is source `5b02f84fc0e18215426c652acfa942cdbae4cbcb`
(`0.5.1`), SHA-256
`03b61a1ef4a948396520b81118e7121b452878a3405c2868fca2ad39df297f67`.
Rollback must restore that pin and the prior direct-Rofi Mod+G invocation
together; the old archive has no initial-frame launcher.
