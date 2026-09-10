# Tmux Session v1 producer contract

This directory is the canonical machine-readable contract owned by Tmux Plus.
It describes the existing public `inventory`, `open`, `create`, `rename`, and
`kill` commands. It adds no runtime schema loading, package import, network
dependency, or private picker protocol.

## Commands and wire envelope

The executable is resolved as `rofi-tmux-plus` through `PATH`. Public commands
use distinct argv elements:

```text
rofi-tmux-plus inventory --json [options]
rofi-tmux-plus open --json --host HOST --server-generation GENERATION \
  --session-id '$6' --created-at SECONDS [options]
rofi-tmux-plus create --json --host HOST --name NAME [options] [-- COMMAND ARG...]
rofi-tmux-plus rename --json --host HOST --server-generation GENERATION \
  --session-id '$6' --created-at SECONDS --expected-name OLD --name NEW
rofi-tmux-plus kill --json --host HOST --server-generation GENERATION \
  --session-id '$6' --created-at SECONDS --expected-name NAME
```

Each public JSON invocation writes one strict UTF-8 JSON document followed by
one LF byte and no other stdout bytes. The complete stdout stream, including
that LF, is at most 1 MiB for inventory and 256 KiB for lifecycle commands.
Stderr is human diagnostics only and is bounded to 64 KiB. A successful
document exits zero. An error document exits nonzero. Numeric exit values and
stderr text are not semantic APIs. Unknown or malformed arguments produce the
`invalid_input` envelope when the producer can initialize.

The producer rejects duplicate object keys and non-finite numbers and never
writes a partial or oversized success document. If response construction or
serialization would exceed a cap, it emits a small `operation_failed` error
instead. Error messages are sanitized and bounded before serialization.

## Structural and semantic validation

The schemas use JSON Schema Draft 2020-12, resolve references only to files in
this directory, and require known v1 fields, types, syntax, and bounds. Object
boundaries remain extensible: unknown object fields are allowed and consumers
must ignore fields they do not use. The error `code` is an open bounded token,
not a closed enumeration; stable meanings are listed below.

Schema validation is structural. The following semantic rules are independent
and are part of this contract:

- `schemaVersion` is 1; inventory always has at least one host and lifecycle
  success has `ok: true`.
- Inventory `generatedAt` and `observedAt` values are Unix milliseconds;
  session `createdAt`, `activityAt`, and `lastAttachedAt` values are Unix
  seconds. All use the same nonnegative 64-bit bound.
- Inventory hosts retain Host Mesh declaration order. A top-level inventory is
  successful even when a host row has `unreachable`, `tmux_missing`, or `error`
  status; those failures remain data. A non-`ok` row has no sessions and has a
  typed error object. An `ok` row is authoritative, including an empty list.
- Inventory contains no more than 128 host rows, 256 sessions per host, or 512
  panes in aggregate per host. Inventory strings are at most 16,384 Unicode
  code points. Lifecycle response strings are at most 4,096 code points unless
  a schema gives a smaller bound. Aggregate byte limits remain authoritative.
- Session identity is the tuple `hostId`, `serverGeneration`, `sessionId`, and
  `createdAt`; names and paths are descriptive values. Session IDs use tmux's
  `$digits` form and pane IDs use `%digits`. `serverGeneration` is opaque and
  contains no control character; whitespace and leading hyphens are preserved
  because the value includes a socket path.
- `meshRevision` is null for local-only operation and otherwise is lowercase
  `sha256:` followed by 64 lowercase hexadecimal digits. A supplied stale
  revision fails before route resolution.
- `open` always returns a complete descriptor and exactly one of `focused` or
  `terminalLaunched` is true. `create` returns its complete descriptor and
  includes those booleans only when `--open` was requested. `rename` returns the
  changed descriptor. `kill` returns the stable reference and the nonnegative
  live client count observed immediately before the operation.
- `expectedName` is an optimistic concurrency guard for rename and kill and is
  optional for open. A mismatch is `stale_session`; an exact name collision is
  `session_exists`. No ambiguous lifecycle failure is retried automatically.
- User options are restricted to `@[A-Za-z0-9_.-]+`; values are clean text.
  Provider-specific option meaning remains outside this contract.

The local-only fallback uses the same case-folded short hostname ID, display,
and full/short hostname aliases as Host Mesh. A missing `rofi-ssh-plus` path is
the only condition that selects this fallback; a resolved but broken provider
is a visible failure.

## Stable error meanings

These meanings are stable for schema version 1. Additional typed codes remain
valid under the bounded error-code rule and are generic visible failures to an
older consumer.

| Code | Meaning |
| --- | --- |
| `unknown_host` | The requested logical host is not in the current Mesh or local identity. |
| `stale_mesh` | The supplied Mesh revision no longer matches current state. |
| `host_unreachable` | No configured remote route reached the selected host. |
| `tmux_missing` | The host was reached but tmux is unavailable. |
| `session_not_found` | The stable session reference no longer exists. |
| `session_exists` | Create or rename would collide with an exact existing name. |
| `stale_session` | The server generation, creation time, expected name, or required option changed. |
| `invalid_input` | A command argument is missing, malformed, unsafe, or out of range. |
| `invalid_cwd` | An explicit creation directory does not exist on the selected host. |
| `launch_failed` | A requested terminal attachment could not be launched. |
| `operation_failed` | A bounded tmux, route, response-construction, or serialization operation failed. |

## Fixtures and bundle digest

`fixtures/index.json` is the conformance index. Every wire case records its
argv command, schema, expected exit class, and accept/reject result. Raw-invalid
cases are byte streams, not purported JSON documents; they cover duplicate keys,
invalid UTF-8, NUL, BOM, non-finite numbers, extra documents, trailing bytes,
missing LF, and extra final LF/space. Each raw case is applicable to inventory
and every lifecycle command. Existing scenario fixtures remain listed as
supporting semantic fixtures.

`SHA256SUMS` contains one LF-terminated line per bundle file, in bytewise
lexicographic order of POSIX relative paths:

```text
<64 lowercase hexadecimal SHA-256>  <relative path>
```

It covers `contract.json`, this document, every schema, the fixture index, and
every fixture including supporting and raw-invalid fixtures. It omits only
itself. The bundle digest is the lowercase SHA-256 hex digest of the exact
UTF-8 bytes of `SHA256SUMS`, including each line's LF. Verifiers reject missing,
extra, duplicate, or unsorted entries before trusting that digest.

Fixtures contain synthetic hosts, routes, session IDs, paths, and provider
options only. Private machine names, credentials, caches, and provider history
do not belong in this public bundle.
