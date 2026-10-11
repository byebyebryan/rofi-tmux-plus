# Exact Observer SDK artifact pin, 2026-10-11

Tmux Plus 0.12.0a6 pins the independently accepted Observer 0.6.0a6 wheel and
all 84 Python modules. Only the artifact pin and package dependency change;
presentation, session contracts, refresh intent and independent action guards
retain their prior code. CI/development downloads derive filenames from the pin.

The source gate passes 293 tests, frozen bundles, guard self-tests and lint.
[The frozen candidate](candidate.json) is built from
`b2afd590997a6747f44b90c904c0284e8aaf66a4`; its wheel SHA-256 is
`d8c03c8f0d2ac1bc71af07b9255c9a50ab66f4613dc8f1b77a14fd7271bae14b`.
[Managed bundle assembly](bundle.json) binds the exact source/native payload.
The descriptor remains `built_unaccepted`; acceptance is recorded separately.

Installed [wheel](headless-wheel.json) and [managed bundle](headless-bundle.json)
headless gates pass with isolated preferences, catalog and a disposable default
tmux server. Actual prepared frame commands and read-only action callbacks retain
rows/filter; explicit refresh reaches terminal feedback; owner outage stays
bounded without a direct fallback. Each gate includes 100 warm frames against
the unchanged 150 ms p95 target. The bundle verifies exact bytes and private
XDG import-cache placement. Complete native references, attachments and hooks
survive cleanup. No compositor, graphical input or physical sleep is exercised.

The paired consumer artifacts are accepted for managed adoption within these
scopes. Observer's package/resource evidence and per-host installed, recovery,
rollback/reselection gates remain independent; see its
[SDK adoption record](https://github.com/byebyebryan/tmux-observer/blob/main/docs/evidence/2026-10-11-mesh-sdk-adoption/README.md).
