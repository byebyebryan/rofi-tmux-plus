# Host Mesh v1 producer fixtures

These fixtures are deliberately synthetic and contain no private machine,
route, or username data. They are producer-owned examples for consumers to
copy into their own conformance tests; consumers must not import
`rofi_ssh_plus` or read the private state files.

`index.json` is the machine-readable conformance index. Documents under
`valid/` are accepted by the listed Draft 2020-12 schema. Streams under
`invalid/` are raw bytes that a consumer must reject before schema validation;
the `.bin` cases intentionally include malformed UTF-8, NUL bytes, duplicate
documents, extra final LF/space bytes, or a missing final LF. `contract.md`
defines the canonical bundle checksum and raw wire rules.

`config-local-only.toml` covers the no-configuration fallback and
`config-multi-route.toml` is the canonical configured mesh. The JSON fixtures
describe deterministic health-report, marker, and envelope cases.
Timestamps use an artificial Unix-millisecond clock so tests do not depend on
wall time.
