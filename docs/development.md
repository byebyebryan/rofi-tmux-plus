# Development

Use Python 3.11+ on Linux, tmux, uv, Ruff 0.16.6 and ShellCheck.
The source gate checks Python behavior, canonical contract bundles, lint,
formatting, shell code and candidate text. It does not start the picker or
establish installed/graphical acceptance.

## Install the pinned producer

Tmux Plus verifies Observer's version, complete Python module coverage and
module hashes before loading prepared, direct or action clients. The source of
truth is [observer-artifact.json](../rofi_tmux_plus/observer-artifact.json).
Installing an arbitrary Observer checkout can fail this check even when its
version string matches.

From the repository root, create a development environment and download the
exact released producer wheel into a private temporary directory:

```sh
uv venv .venv
export TMUX_OBSERVER_WHEEL="$(mktemp -d)/tmux_observer-0.6.0a2-py3-none-any.whl"
python3 - <<'PY'
import hashlib
import json
import os
import urllib.request
from pathlib import Path

pin = json.loads(Path("rofi_tmux_plus/observer-artifact.json").read_text())
wheel = Path(os.environ["TMUX_OBSERVER_WHEEL"])
url = (
    "https://github.com/byebyebryan/tmux-observer/releases/download/v"
    + pin["version"] + "/" + wheel.name
)
with urllib.request.urlopen(url, timeout=30) as response:
    raw = response.read(4 * 1024 * 1024 + 1)
assert len(raw) <= 4 * 1024 * 1024, "Observer artifact exceeds bound"
assert hashlib.sha256(raw).hexdigest() == pin["wheelSha256"], "Observer artifact drift"
wheel.write_bytes(raw)
PY
uv pip install --python .venv/bin/python --no-deps "$TMUX_OBSERVER_WHEEL" ruff==0.16.6
```

The current pin is Observer **0.6.0a2**. When deliberately updating it, use the
new exact wheel filename and repeat the producer's independent gates before
consumer selection. Keep the wheel available for explicit package acceptance;
the installed module pin and wheel digest are different verification scopes.

## Run the source gate

`scripts/check` uses `python3` and `ruff` from PATH. Select the environment:

```sh
PATH="$PWD/.venv/bin:$PATH" ./scripts/check
```

An already installed exact Observer payload is sufficient for source tests.
[CI](../.github/workflows/check.yml) downloads the same pinned wheel and verifies
its digest before the gate. Canonical producer/consumer bundle synchronization
is also available as `./scripts/check-contract-sync`; it requires compatible
public producer commands on PATH.

For source CLI diagnosis with that environment:

```sh
PYTHONPATH=. .venv/bin/python bin/rofi-tmux-plus inventory --json
```

That command performs an explicit fresh read. Source picker use additionally
requires the accepted native mode, supported Rofi binary and running prepared
services; see the [launcher invocation](../README.md#launch-the-picker).

## Package and acceptance boundaries

`scripts/candidate-artifact` builds frozen committed-source candidates.
`scripts/release-bundle` assembles the verified native/source managed bundle.
Neither operation accepts or selects a runtime merely by building it.

`scripts/accept-headless-picker` exercises actual installed frames and callbacks
with isolated preferences/cache and owned disposable sessions.
`scripts/accept-native-picker` adds the separate graphical scope and requires an
available desktop. Native/resource, managed bytes, recovery and rollback remain
independent producer/consumer gates. See [Mesh selection](mesh-integration.md)
for the exact accepted tuple and [documentation index](README.md) for history.

The frozen implementation under `tests/reference_frontend` is a historical
regression baseline. Its tests do not establish acceptance of replacement code.

## Owner conformance tooling

`scripts/check` runs this project's source and synthetic guard checks.
`scripts/check-public-contract` runs isolated public CLI probes: SSH owns Host
Mesh; Tmux supplies `--ssh-checkout /absolute/ssh`; Agent supplies that argument
and `--tmux-checkout /absolute/tmux`. Checkouts must be clean. Run with the
project's selected Python environment and declared dependency versions. The
probe uses a private HOME/XDG/PATH, synthetic local-only alpha authority, empty
tmux state and provider stubs. It imports no sibling implementation and inspects
no ordinary provider sessions. Consumer bundle-sync gates retain canonical
producer ancestry, historical/current manifests and vendored provenance.

`scripts/check-installed --self-test` proves owned callback and parser guards.
Explicit `--live` checks installed public behavior with isolated UI state and
semantic SSH history preservation. Tmux's separate `--lifecycle` option owns
uniquely named disposable sessions, exact reference cleanup, stale-name and
collision guards and baseline preservation. Run it only for authorized lifecycle
acceptance; pure relocation does not need it. SSH probes no legacy custom-connect
mutation. Source/synthetic, installed, provider actions, graphical/focus and
physical acceptance remain separate.
