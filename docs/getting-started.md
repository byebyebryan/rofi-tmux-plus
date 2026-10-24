# Getting started with Tmux Plus

Tmux Plus has two entry paths: a fresh public CLI and a graphical picker backed
by prepared cached services. Use the path your task needs; a missing prepared
service does not switch the picker into fresh collection.

## Prerequisites and artifacts

Use Linux, Python 3.11+, tmux, Rofi and the supported native-mode/Rofi binary
tuple. Remote use additionally requires configured SSH Plus Host Mesh routes
and the owning host helpers/services. Each endpoint's catalog is directional.

The [v0.12.0a8 release](https://github.com/byebyebryan/rofi-tmux-plus/releases/tag/v0.12.0a8)
publishes the qualified bundle, checksums and provenance, paired with exact
Observer 0.6.0a9 bytes. It is bundle-only, with no separately published Plus
wheel. Preserve the bundle's producer and native tuple; installing an arbitrary
same-version Observer checkout is insufficient.

For a checkout environment, [development](maintenance/development.md#install-the-pinned-producer)
gives a complete hash-verified producer install and source gate. Native bundle
preparation and verification are separate steps described there; source checks
alone do not produce a usable graphical installation.

## Explicit CLI inspection

With the accepted public command on PATH:

```sh
rofi-tmux-plus inventory --json
rofi-tmux-plus inventory --json --with-viewers
```

These perform explicit fresh reads. The optional viewer extension reports this
caller's open/none/unknown evidence without changing owner facts or supplying
action handles. Lifecycle operations need exact references and independent
guards; see [Tmux Session v1](reference/tmux-session-v1.md).

## Prepared picker

Host the Observer owner and prepared fleet/desktop services explicitly before
launching the picker. See [Observer setup](https://github.com/byebyebryan/tmux-observer/blob/main/docs/getting-started.md),
[service contexts](https://github.com/byebyebryan/tmux-observer/blob/main/docs/guides/service-contexts.md)
and [Mesh selection](guides/mesh-integration.md). Package installation starts none of
those services. The prepared reader must belong to this desktop's context.

The [usage guide](guides/usage.md#launch-the-picker) provides the full source launcher
invocation, native guard behavior, keys and failure states. The launcher owns
its watch cleanup; it does not own ordinary tmux sessions merely by listing them.

Managed pins, installed readiness and recovery belong to the deployment owner
and [Observer operations](https://github.com/byebyebryan/tmux-observer/blob/main/docs/maintenance/operations.md).
An old graphical or native receipt retains its original exact scope.
