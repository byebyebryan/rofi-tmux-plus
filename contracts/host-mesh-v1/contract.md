# Host Mesh v1 producer contract

This directory is the canonical machine-readable Host Mesh v1 contract owned
by SSH Plus. It describes the existing `mesh list --json` and
`mesh report-route --json` process boundary. It does not add a discovery call,
runtime schema loading, a package import, or a network dependency.

## Commands and envelopes

The executable is resolved as `rofi-ssh-plus` through `PATH`. Commands are
argv elements, not shell strings:

```text
rofi-ssh-plus mesh list --json
rofi-ssh-plus mesh report-route --json --host HOST --route ROUTE \
  --status reachable|unreachable --source SOURCE \
  --mesh-revision REVISION --observed-at MILLISECONDS
```

Each command writes one JSON document encoded as strict UTF-8 followed by one
LF byte and no other bytes. The final LF is included in the 512 KiB stdout
limit. A successful document exits zero. An error document exits nonzero.
Numeric nonzero exit values and stderr text are not semantic APIs. Stderr is
human diagnostics only and is bounded to 64 KiB.

If the producer can initialize, malformed or unknown command arguments produce
the `invalid_input` error envelope. The producer never writes a partial or
oversized document. If configuration is individually valid but the complete
list response exceeds the stdout profile, it emits a small
`invalid_config` envelope instead. A response-construction failure emits a
small `persistence_failed` envelope.

The error envelope has the following required fields:

```json
{"schemaVersion":1,"ok":false,"error":{"code":"invalid_input","message":"..."}}
```

The success envelope for route reports is:

```json
{"schemaVersion":1,"ok":true,"accepted":true}
```

`accepted` is false when the report is not newer than the newest recorded
outcome for the route. It is still a successful, zero-exit response.

## Resource profile

The producer enforces the following v1 profile before emitting a document:

| Resource | Limit |
| --- | ---: |
| Complete stdout document including LF | 524,288 bytes |
| Stderr | 65,536 bytes |
| Hosts in a list response, including the local host | 128 |
| General string values | 16,384 Unicode code points |
| Error messages | 4,096 Unicode code points |
| Route-report source labels | 64 Unicode code points |
| Unix-millisecond timestamps | 0 through 9,223,372,036,854,775,807 |

SSH policy keeps its existing smaller limits: connection timeout is 1 through
60 seconds, connection attempts is 1 through 10, and route-health TTL is 1
through 86,400 seconds. Host IDs remain ASCII identifiers matching
`[A-Za-z0-9][A-Za-z0-9_.-]*`. Tokens contain no whitespace or control
characters and do not begin with `-`. Displays contain no control characters.

The schema files publish structural limits. The aggregate stdout limit remains
authoritative when nested host, alias, route, or extension arrays make a
response too large.

## Extensibility and validation layers

Known fields are required and have fixed types, syntax, and bounds. Object
boundaries are extensible: unknown object fields are allowed and consumers
ignore fields they do not use. Arrays remain bounded where the schema states a
maximum. The error `code` is intentionally an open bounded token (a letter
followed by letters, digits, `_`, `.`, or `-`, at most 64 code points), not a
closed enumeration. An older consumer treats an unknown code as a generic
visible failure.

JSON Schema validates structure: required fields, primitive types, field
bounds, and local references. It does not replace the semantic rules below.
Producer and consumer tests must run both layers, including independent raw
wire checks for strict UTF-8, one document, one final LF, duplicate keys, and
trailing bytes.

## `mesh list` semantics

`schemaVersion` is 1. `generatedAt` and all route-health timestamps are
nonnegative Unix milliseconds. `meshRevision` is the producer's lowercase
`sha256:` prefix followed by 64 lowercase hexadecimal characters,
representing the digest of normalized identity, route, and SSH-policy
configuration. Usage history and route-health changes do not change it.

There is exactly one local host, it is first, and its ID equals
`localHostId`. Local hosts have no routes. Remote hosts follow declaration
order and each has at least one route. Host IDs, aliases, and routes are
case-folded for identity and correlation; display and route spelling are
preserved. Every identity value maps to at most one logical host.

Each route's `configuredIndex` is its deterministic declaration index. The
`routes` array is the current recommended attempt order. A route whose newest
health observation is `unreachable` is moved behind healthy routes for the
configured TTL. After that TTL, configured order is restored. Positive health
reports do not permanently promote a fallback route.

## `mesh report-route` semantics

The host must be an existing remote logical ID and the route must be one of
that host's configured routes. `status` is `reachable` or `unreachable`.
`source` is a bounded diagnostic label and has no effect on SSH history.
`meshRevision` must exactly equal the current revision, otherwise the typed
`stale_mesh` error is returned. `observedAt` is a nonnegative Unix-millisecond
timestamp no more than five minutes ahead of the receiver's clock. Reports
are stored monotonically per host and route; duplicates and older reports
return `accepted: false` without mutation.

Route health is separate from explicit SSH usage history. Reports never
increment connection counts or update `lastConnected`.

## Stable error meanings

These meanings are stable for schema version 1. The schema accepts additional
typed codes under the bounded error-code rule above.

| Code | Meaning |
| --- | --- |
| `invalid_input` | Missing, malformed, unknown, or out-of-range command input. |
| `invalid_config` | Present Mesh configuration is invalid or cannot fit the published response profile. |
| `unsupported_schema` | Present configuration requests a Host Mesh schema version this producer does not implement. |
| `unknown_host` | A route report names no existing remote logical host. |
| `unknown_route` | A route report names no route configured for its host. |
| `stale_mesh` | A report revision does not match the current Mesh revision. |
| `persistence_failed` | Route-health persistence or response construction failed. |

Only these typed meanings can influence a consumer's documented recovery. A
transport failure, timeout, malformed output, exit/body mismatch, overflow,
unknown code, or stderr message never authorizes an automatic action retry.

## Fixture and bundle rules

`fixtures/index.json` is the conformance index. Every wire case records its
argv command, fixture path, document schema, expected exit class, and expected
accept/reject result. Raw-invalid cases are byte streams and are never
treated as purported JSON documents. Existing semantic fixtures remain in the
bundle and are listed as supporting fixtures.

`SHA256SUMS` uses one LF-terminated line per file, in deterministic bytewise
lexicographic order of POSIX relative paths:

```text
<64 lowercase hexadecimal SHA-256>  <relative path>
```

It covers `contract.json`, `contract.md`, every schema, the fixture index, and
every fixture including supporting and raw-invalid fixtures. It omits only
itself. The bundle digest is the lowercase SHA-256 hex digest of the exact
UTF-8 bytes of `SHA256SUMS`, including each line's LF. No comments, blank
lines, absolute paths, or generated timestamps are permitted. A verifier
must reject missing, extra, duplicate, or unsorted entries before trusting the
digest.

Fixtures and metadata contain synthetic names only. Private hostnames,
routes, usernames, credentials, local state paths, and runtime caches do not
belong in this public bundle.
