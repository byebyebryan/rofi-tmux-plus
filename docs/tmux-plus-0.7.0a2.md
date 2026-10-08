# Tmux Plus 0.7.0a2 browse renewal repair

Cycling views could mark every session unavailable and empty Open/Attached even
while both Observer services were healthy. The callbacks reused a persisted
presentation whose original owner/desktop leases had expired. Quiet watch
renewals intentionally do not redraw Rofi, so that presentation can outlive its
leases. The all-row warning therefore described an old frame rather than the
reader's current facts.

Browse arrows and action cycling now read the current prepared local cache and
persist the adopted frame. They admit no refresh, activate no service, perform
no tmux/SSH collection, and instantiate no lifecycle service. Confirmation
callbacks remain inert and retain their exact frozen target. Failed reads retain
the old rows with a bounded visible notice and normal expiry checks; watch
metadata cannot renew expired owner or desktop authority.

The runtime freeze is `388ee6e466ef5321797f495792dac52ab83d230d`. Observer remains
the accepted `0.1.0a1` wheel. The native library remains byte-identical to the
previously accepted `0.7.0a1` integration. The immutable
[candidate descriptor](evidence/2026-10-08-browse-renewal/frontend-candidate.json)
retains `built_unaccepted`; the checks below are separate scoped evidence.

The full source gate passes 281 tests, compile, canonical bundles, Ruff,
ShellCheck and candidate text checks. Regressions cover both arrow directions,
both action directions, adoption after quiet renewal, persisted new leases,
expired facts despite healthy watch metadata, failed reader access, and frozen
confirmations. Source [CI passes](https://github.com/byebyebryan/rofi-tmux-plus/actions/runs/37848734418).

The [native fleet reproduction](evidence/2026-10-08-browse-renewal/live-renewal.json)
holds a presentation for 12 seconds with an owned read-only watch. On Snap the
old implementation renders 15 warning rows despite healthy current services.
The repaired source on Snap and installed candidate wheel on Starship retain
all 15 healthy rows and correct Open/Attached membership after view cycling.
Starship's watch sequence remains unchanged across the hold, directly exercising
quiet renewal. Preferences/cache are isolated; ordinary sessions are untouched.

The [Starship checkpoint](evidence/2026-10-08-browse-renewal/starship-checkpoint.json)
passes 100 headless installed frames at p95 127.22 ms, below the 150 ms target.
Two graphical attempts stop before input because DMS's display-idle overlay holds
exclusive focus. Temporary idle inhibition is restored. Fresh graphical
acceptance of this frontend is unrun; the committed harness adds a browse-cycle
check after the original startup leases expire for the next available window.
Snap receives no graphical launch or input. Neither host is suspended.
