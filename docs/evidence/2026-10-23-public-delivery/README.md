# Tmux Plus 0.12.0a8 bundle-only public supply preparation

Prepared 2026-10-23 for the selected Mesh a24 full-view cohort. At preparation
time the public `v0.12.0a8` tag and GitHub prerelease were absent. This record
prepares the exact selected bundle and its candidate metadata; it does not
claim a push, tag or publication.

## Release assets

| Asset | Bytes | SHA-256 | Prepared source |
| --- | ---: | --- | --- |
| `candidate.json` | 23,394 | `57ef5392a0758e8c71452ea3debfe71d4fbc07607ed6388e23328ce866dff761` | `docs/evidence/2026-10-20-mesh-a24-full-view/frontend-candidate.json` |
| `rofi-tmux-plus-0.12.0a8-linux_x86_64.zip` | 2,321,433 | `1fb86a14ad7e5840e179bf2d67824023d79f96c3e76f2f214ab6399a14a74a89` | `/home/bryan/.local/share/tmux-observer/artifacts/1fb86a14ad7e5840e179bf2d67824023d79f96c3e76f2f214ab6399a14a74a89/` |

This a8 prerelease is bundle-only: `candidate.json` plus the exact managed
source/native bundle zip. It does not offer a standalone `.whl` download. The
embedded `release-manifest.json` has SHA-256
`0ba01dcd7e512cdbdc98576ca12d200e26eb551d4cf2f03ac3f46c7fb4a5aea2` and
enumerates the other 382 bundle files. The deployment selection verifies all
383 archive members, including that embedded manifest.

The candidate descriptor records the inner build wheel
`rofi_tmux_plus-0.12.0a8-py3-none-linux_x86_64.whl` (80,620 bytes,
SHA-256 `bc1842dd025f20ffed05a713c70e5b7b0af58473b46de5c6a076abbe440f4fc4`).
That digest is build provenance only; the wheel is not a release asset and has
not been reconstructed or rebuilt.

## Provenance and release order

- Runtime package and selected artifact review source:
  `3839c2a22a4c33b690896c3497436a9b32855a1d`.
- Managed full-view qualification record:
  `a71a4d55bb6f428fc142caf6422f60424425173b` in Tmux Observer.
- The release source tip may be later than the wheel source. The exact tag
  target is the prepared source branch tip reported with the local handoff; do
  not relabel the candidate's `source.commit` as that tag target.
- Publish the exact Tmux Observer `0.6.0a9` wheel first. The Plus hosted
  workflow downloads that versioned asset and checks SHA-256
  `b117cc5d353f323ce25c6629dc3371d5d95aa349639cf09974fda3a62f8d7738`.
  Preserve this check; local consumer tests do not establish hosted CI.

The [2026-10-20 full-view acceptance](https://github.com/byebyebryan/tmux-observer/blob/a71a4d55bb6f428fc142caf6422f60424425173b/docs/evidence/2026-10-20-mesh-a24-full-view/README.md)
owns the installed scope. This release preparation does not enable view-feed or
semantic delta.
