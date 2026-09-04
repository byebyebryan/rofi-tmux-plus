# Tmux Session Contract v1

Status: proposed; no released `rofi-tmux-plus` version implements this
contract.

This is the process boundary between generic tmux lifecycle and consumers such
as `rofi-agent-plus`. All commands exchange versioned JSON. Consumers do not
import Tmux Plus internals, invoke raw helper scripts from its package, or read
its cache files.

## Dependencies and host identity

Logical hosts and remote route candidates come from
[Host Mesh Contract v1](https://github.com/byebyebryan/rofi-ssh-plus/blob/main/docs/HOST_MESH_V1.md).
Every public operation accepts a logical `hostId`; it does not accept a cached
route endpoint as host identity.

When a compatible Host Mesh provider is unavailable, Tmux Plus synthesizes the
local host and supports local operations only. The fallback ID and display are
the case-folded short system hostname, and the full and short hostnames are
aliases, matching Host Mesh's no-configuration rule. An unknown nonlocal host
is an error rather than an invitation to interpret arbitrary SSH input.

The canonical commands `rofi-ssh-plus` and `rofi-tmux-plus` are resolved
through `PATH`. A suite deployment installs their public entry points
independently of Rofi's script-mode symlinks. Callers may treat a missing SSH
Plus as local-only capability, but malformed output or an unsupported schema is
not equivalent to an absent command.

## Session descriptor and reference

A session descriptor has this shape:

```json
{
  "hostId": "desktop-a",
  "serverGeneration": "tmux-v1:1722741000:1234:/run/user/1000/tmux-1000/default",
  "sessionId": "$6",
  "createdAt": 1722742000,
  "name": "rofi-tmux-plus",
  "activityAt": 1722743000,
  "lastAttachedAt": 1722742900,
  "attachedClients": 2,
  "pending": false,
  "windowCount": 1,
  "sessionPath": "/home/user",
  "currentWindow": "editor",
  "currentPath": "/home/user/code/rofi-tmux-plus"
}
```

Tmux timestamps are Unix seconds. Missing tmux format values become JSON
`null`; they are not silently changed to zero or the current time.
`pending` is true only for a Tmux Plus session whose deferred command is still
waiting for its first client attachment.

The stable reference used by lifecycle operations is:

```json
{
  "hostId": "desktop-a",
  "serverGeneration": "tmux-v1:1722741000:1234:/run/user/1000/tmux-1000/default",
  "sessionId": "$6",
  "createdAt": 1722742000
}
```

`serverGeneration` is an opaque value derived from the live default server's
socket path, server start time, and PID. Tmux session IDs are unique only
within that server generation. The generation, session ID, and creation time
form the durable action identity; after validation, the session ID is used as
the exact tmux target.

Lifecycle requests may add `expectedName` as an optimistic-concurrency
precondition; it is not part of the reference. It is required for rename and
kill and optional for open. When supplied, a name mismatch returns
`stale_session`. Open normally omits it so a benign external rename does not
prevent opening the same proven session.

## Live inventory

```text
rofi-tmux-plus inventory --json \
  [--host HOST_ID]... \
  [--mesh-revision REVISION] \
  [--panes] \
  [--session-option @NAME]...
```

Inventory performs a bounded live read. It does not return retained cached
sessions. With no `--host`, it queries the local host and every configured
remote host concurrently. Repeated `--host` limits the request while retaining
mesh declaration order.

Successful output has this shape:

```json
{
  "schemaVersion": 1,
  "generatedAt": 1722743000123,
  "meshRevision": "sha256:0123456789abcdef",
  "hosts": [
    {
      "hostId": "desktop-a",
      "display": "Desktop A",
      "local": true,
      "status": "ok",
      "observedAt": 1722743000123,
      "nativeHostname": "desktop-a-native",
      "serverGeneration": null,
      "route": null,
      "sessions": []
    }
  ]
}
```

`meshRevision` is copied from the Host Mesh observation used for the request.
It is `null` in synthesized local-only mode. When a caller supplies
`--mesh-revision`, a different current revision returns `stale_mesh` rather
than mixing host sets from two configurations. Agent Plus supplies both that
revision and one repeated `--host` for every host in its provider-discovery
set.

Every lifecycle operation also accepts `--mesh-revision`. Tmux Plus and Agent
Plus supply the revision associated with the selected live or cached row. A
remote operation rejects a stale revision before route resolution; a local-only
operation with no Host Mesh provider uses `null` and omits the argument.

Host status is one of:

- `ok`: the host was reached and the default tmux inventory is authoritative;
- `unreachable`: no route reached the host within policy;
- `tmux_missing`: the host was reached but tmux is unavailable; or
- `error`: the host was reached but inventory could not be interpreted.

`ok` with an empty `sessions` array includes both a missing tmux server and a
running server with no sessions. Those are authoritative empty inventories,
not failures. `serverGeneration` is `null` when no server is running and is
present for every observation of a running server. For other statuses,
`sessions` is empty and an `error` object with bounded `code` and `message`
fields is present.

The `route` field is the SSH destination that produced a valid version-1
reached-host marker for that live observation. It is transport metadata, not
identity. Tmux Plus reports the corresponding revisioned, event-timed
route-health observation to SSH Plus.

When `--panes` is requested, each session also contains:

```json
"panes": [
  {
    "paneId": "%1",
    "pid": 12345,
    "currentPath": "/home/user/code/project",
    "currentCommand": "codex"
  }
]
```

When `--session-option @NAME` is repeated, each descriptor contains an
`options` object with every requested key. An absent option has value `null`.
Only valid tmux user-option names matching `@[A-Za-z0-9_.-]+` are accepted.
This lets Agent Plus request its provider IDs without making Tmux Plus aware
of any provider.

Per-host failures are data, so a structurally valid inventory response exits
zero even when one or every host has a non-`ok` status. Invalid arguments,
mesh/configuration failure, or an inability to construct the response returns
nonzero with the standard error envelope.

## Open

```text
rofi-tmux-plus open --json \
  --host HOST_ID \
  [--mesh-revision REVISION] \
  --server-generation GENERATION \
  --session-id '$6' \
  --created-at 1722742000 \
  [--expected-name rofi-tmux-plus]
```

Open revalidates the stable reference, then focuses a matching Niri terminal
or launches a new terminal attachment. A launched terminal is always detached
from the command's lifecycle so the JSON command can return immediately.
Successful output is:

```json
{
  "schemaVersion": 1,
  "ok": true,
  "meshRevision": "sha256:0123456789abcdef",
  "session": {
    "hostId": "desktop-a",
    "serverGeneration": "tmux-v1:1722741000:1234:/run/user/1000/tmux-1000/default",
    "sessionId": "$6",
    "createdAt": 1722742000,
    "name": "rofi-tmux-plus",
    "activityAt": 1722743000,
    "lastAttachedAt": 1722742900,
    "attachedClients": 2,
    "pending": false,
    "windowCount": 1,
    "sessionPath": "/home/user",
    "currentWindow": "editor",
    "currentPath": "/home/user/code/rofi-tmux-plus"
  },
  "focused": true,
  "terminalLaunched": false
}
```

Exactly one of `focused` or `terminalLaunched` is true on success.
`session` is always the complete descriptor shape used by inventory and create.
`terminalLaunched` confirms a successful local spawn, not a completed SSH
attachment.

## Create

```text
rofi-tmux-plus create --json \
  --host HOST_ID \
  [--mesh-revision REVISION] \
  --name NAME \
  [--cwd PATH] \
  [--set-option @NAME=VALUE]... \
  [--defer-until-attached] \
  [--attach-timeout SECONDS] \
  [--open] \
  [-- COMMAND ARG...]
```

Create has strict automation semantics: an exact existing name returns
`session_exists`. It does not attach to or mutate that session. Without a
command, tmux starts the host's default shell. With `--`, every remaining local
argument is one command argv element; Tmux Plus performs any required remote
quoting.

Without `--cwd`, creation starts in the selected host user's home directory,
independent of the launcher's current directory. An explicit path must exist
as a directory on that host or creation returns `invalid_cwd`; Tmux Plus never
silently substitutes another directory. Agent Plus decides whether a stale
provider path should be replaced with the host home before calling create.

Only tmux session user options matching `@[A-Za-z0-9_.-]+` may be set. Name,
path, option values, and command arguments reject NUL and control characters;
tmux remains authoritative for additional name and path validity.

`--defer-until-attached` is valid only with a command. Tmux Plus creates a
pending session whose wrapper waits for the first tmux client, clears its
private pending marker, and then executes the supplied argv. The wait is
bounded by `--attach-timeout`, otherwise by the configured attach timeout,
which defaults to 60 seconds. If no client attaches, the wrapper exits and tmux
removes the otherwise empty session. Inventory exposes this state through the
provider-neutral `pending` field.

Creation, option setup, command setup, and response discovery form one
operation. The implementation:

1. acquires the per-host mutation lock and generates an operation token;
2. creates the detached session with an internal holding wrapper and obtains
   its session ID and creation time directly from tmux;
3. records the operation token and requested user options on that exact
   session;
4. validates and captures its complete descriptor;
5. releases the wrapper to the default shell or supplied argv, or leaves it at
   the bounded first-client gate when deferred; and
6. clears the operation token after successful setup.

On failure before release, cleanup kills the new session only after its server
generation, session ID, creation time, and operation token still match. It
never removes a pre-existing or externally replaced session. `--open` performs
the normal open lifecycle after creation; with a deferred command, the new
client releases the wait gate. The response contains the complete created
session descriptor plus `focused` and `terminalLaunched` when opening was
requested.

The interactive picker may implement friendlier ensure-and-open behavior by
looking up an exact name first. That behavior is not the automation contract.

## Rename

```text
rofi-tmux-plus rename --json \
  --host HOST_ID \
  [--mesh-revision REVISION] \
  --server-generation GENERATION \
  --session-id '$6' \
  --created-at 1722742000 \
  --expected-name OLD_NAME \
  --name NEW_NAME
```

Rename revalidates the reference and returns the updated descriptor. A target
name collision is `session_exists`; a reference mismatch is `stale_session`.

## Kill

```text
rofi-tmux-plus kill --json \
  --host HOST_ID \
  [--mesh-revision REVISION] \
  --server-generation GENERATION \
  --session-id '$6' \
  --created-at 1722742000 \
  --expected-name NAME
```

Kill revalidates the reference immediately before destroying it. Confirmation
is a picker responsibility; this automation command assumes its caller has
already obtained authorization. Success returns the killed reference and the
live client count observed immediately before the operation.

## Result and error envelope

Every JSON command writes exactly one JSON document to stdout. Successful
mutation responses include `schemaVersion`, `ok: true`, and the affected
identity or descriptor. When Host Mesh is available they also echo the
`meshRevision` used for route resolution. An operation failure returns nonzero
and writes:

```json
{
  "schemaVersion": 1,
  "ok": false,
  "error": {
    "code": "stale_session",
    "message": "the selected tmux session changed; refresh and try again",
    "hostId": "desktop-a"
  }
}
```

Stable error codes for version 1 are:

- `unknown_host`;
- `stale_mesh`;
- `host_unreachable`;
- `tmux_missing`;
- `session_not_found`;
- `session_exists`;
- `stale_session`;
- `invalid_input`;
- `invalid_cwd`;
- `launch_failed`; and
- `operation_failed`.

Human diagnostics may also go to stderr. Dynamic remote stderr is bounded and
sanitized before inclusion in JSON. Consumers ignore unknown fields within
schema version 1 and reject unsupported schema versions.

## Security and concurrency

- Row text is never accepted as operation identity.
- Remote commands quote every dynamic value and validate reached-host
  markers exactly as specified by Host Mesh v1 before classifying route
  health.
- Background and management SSH is noninteractive and bounded.
- Mutation validation and action occur in one local or remote transaction,
  minimizing time-of-check/time-of-use races.
- A process-local or filesystem lock serializes conflicting Tmux Plus
  mutations for the same logical host. Tmux remains the final concurrency
  authority.
- The contract never accepts arbitrary SSH options, alternate tmux socket
  flags, or shell command strings.

## Agent Plus consumption

Agent Plus requests panes and only these session options initially:

```text
@codex_thread_id
@codex_name
@claude_session_id
@claude_name
@opencode_session_id
@opencode_name
@agent_picker_waiting
```

`@agent_picker_waiting` is requested only to recognize wrappers created by the
pre-contract `rofi-agent-picker`. New wrappers use the generic `pending`
descriptor field and Tmux Plus's private marker.

It correlates provider-native sessions with the returned generic tmux
inventory. It calls `open` for an existing reference and `create` with a
provider argv and user options for a new wrapper session. Provider identity,
waiting-session reuse, and detection of agents outside tmux remain Agent Plus
responsibilities.

## Required conformance fixtures

The version-1 implementation publishes deterministic JSON and command fixtures
covering:

- local inventory with no server, a running empty server, and multiple
  sessions;
- mixed local and remote inventory with one unavailable host;
- requested pane metadata, present and absent user options, and pending state;
- `stale_mesh` before route resolution;
- a server restart reusing a session ID without matching the old generation;
- external rename behavior for open versus rename and kill;
- successful default-shell and deferred-command creation;
- name collision, invalid directory, setup rollback, and deferred timeout; and
- the standard success and every stable error envelope.

Contract-consumer tests use these published fixtures and executable fakes. They
do not import Tmux Plus Python modules or rely on a developer's live tmux
server.
