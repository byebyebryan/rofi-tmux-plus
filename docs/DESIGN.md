# Design: rofi-tmux-plus

Status: P6 local and Host Mesh-backed remote lifecycle and live inventory, the
private retained remote cache and refresh lifecycle, the complete Rofi
browse/open/create/rename/kill UI, fail-closed callback recovery, deployment,
and automated fleet acceptance are complete; operator acceptance is complete on
Snap and Starship, while Carbon is in a daily-drive soak for the published
suite/P9 behavior. P7 removed the
redundant
picker-model read before a successful typed open; lifecycle still revalidates
the current Mesh and exact stable reference. The coordinated P8 flat-scope
navigation cutover is published and deployed, with operator acceptance
complete on Snap and Starship; Carbon's soak applies to that published
behavior. The P9
producer and consumer implementation and canonical bundles are published in
this repository; managed suite deployment is coordinated through chezmoi.

## P9 locked CLI contracts

Tmux Plus has both P9 roles. As a Host Mesh consumer, it vendors the complete
canonical Host Mesh v1 bundle with one exact producer-provenance record, then
continues to parse the subprocess output independently. As the Tmux Session
producer, it publishes a complete bundle of canonical Draft 2020-12 schemas,
machine metadata, normative semantic rules, checksums, and synthetic valid plus
raw-invalid fixtures for inventory and every lifecycle response.

P9 keeps the existing executable boundary through `PATH`; it adds no Python
dependency on SSH Plus and no static remote-host fallback. Failure to resolve an
executable SSH Plus command continues to select the existing local-only
identity. Once a path resolves, launch failure, disappearance, oversized or
malformed output, and incompatibility remain visible failures.

Inventory and lifecycle stdout remain one strict UTF-8 JSON document followed
by exactly one LF. Stderr is bounded diagnostics only, numeric nonzero exit
codes carry no domain meaning, and only a matching typed error/nonzero-exit pair
may influence documented pre-action recovery. Ambiguous transport or response
failure never repeats a lifecycle action. Per-host inventory failures remain
data in a successful top-level inventory.

The P9 artifacts describe and test the current v1 semantics; they do not add a
runtime handshake or reopen P8 navigation. The coordinated suite design and
rollout boundary live in the managed `rofi-plus-p9-cli-contracts.md` document.

P9 itself did not change picker presentation. A subsequent post-P9 SSH-only
refinement makes SSH recent-only and restores its native filter arrows; it
leaves Tmux behavior, Host Mesh v1, and both P9 wire contracts unchanged. That
SSH refinement remains a separate candidate requiring publication, deployment,
and acceptance.

## P8 flat-scope implementation

P8 replaces the previously deployed `Recent` / `Hosts` root pair and per-host
child layers with leaf-only peer views:

```text
Tmux › All
Tmux › Local
Tmux › <remote host in Host Mesh order>
```

`All` is the mixed, recency-ordered cross-host session list. `Local` follows,
then every authoritative remote in stable Host Mesh order; availability and
activity never reorder the ring. Empty and unavailable hosts keep their scope.
When no remote exists, the redundant `All` and `Local` scopes collapse to one
`Local` view. Tmux Plus starts in `All` when it exists and does not persist a
host scope between invocations.

Left and Right wrap through scopes without discovery or network work, preserve
the current filter, and reset selection to the first eligible matching row.
Tab and Shift+Tab retain native row navigation. Enter opens the selected
session. Escape and Ctrl+G always close through Rofi's native cancel action and
are never script callbacks.

Each model render is also written as a private, content-addressed presentation
snapshot. The continuation state carries only its opaque snapshot key. Left and
Right callbacks load that exact key and do not construct configuration, read
Host Mesh, inspect local tmux, or call lifecycle code. A missing, corrupt, or
unsafe snapshot fails closed with a bounded notice so the picker can be reopened.
The cache retains the newest 256 owned snapshots (plus the snapshot being
written), which bounds disk growth while leaving room for concurrent and
long-lived picker windows.

Ctrl+Enter creates or opens a typed session only from a concrete host scope.
From `All`, it renders the bounded instruction `Choose a concrete host view
before creating a session.` rather than guessing a host or entering a chooser
layer. Rename and kill confirmation stay transient action states; Left and
Right do nothing there, and native Escape closes the picker without committing
the action. Host Mesh v1 and Tmux Session v1 do not change.

## Product boundary

`rofi-tmux-plus` is a fast, searchable manager for tmux sessions on the local
host and explicitly configured SSH peers. The primary object is a tmux session
on a logical host. SSH is transport; provider-specific agent history is a
higher layer.

The project owns generic tmux inventory and lifecycle:

- list and filter sessions;
- focus a matching local Niri terminal when possible;
- attach locally or through SSH;
- create a session;
- rename a session; and
- kill a session after confirmation.

It does not identify Codex, Claude Code, or OpenCode sessions or decide how
those providers resume. `rofi-agent-plus` owns that policy and consumes the
generic [Tmux Session Contract v1](TMUX_SESSION_V1.md).

Remote hosts come from the implemented
[Host Mesh Contract v1](https://github.com/byebyebryan/rofi-ssh-plus/blob/main/docs/HOST_MESH_V1.md).
Tmux Plus does not maintain a second list of aliases or SSH routes. It remains
useful in local-only mode when SSH Plus is absent; remote capability requires a
compatible Host Mesh provider.

The public `rofi-tmux-plus` and `rofi-ssh-plus` executables are resolved through
`PATH`. Suite deployments install them under `~/.local/bin` or an equivalent
user executable directory; Rofi script-mode symlinks are not used as private
cross-project API paths.

## Configuration and local integration

Configuration is optional at
`${XDG_CONFIG_HOME:-~/.config}/rofi-tmux-plus/config.toml`. Version 1 accepts:

```toml
schema_version = 1
terminal = ["ghostty"]
refresh_seconds = 30
attach_timeout_seconds = 60
```

`terminal` is a nonempty argv prefix, not a shell command string. Tmux Plus
appends `-e` and its exact local or SSH attachment argv. The default is
`["ghostty"]`. Values containing NUL or control characters are invalid.
`refresh_seconds` controls picker-cache freshness; bounded SSH connection
policy remains owned by Host Mesh. `attach_timeout_seconds` controls the
provider-neutral first-client gate used by programmatic creation.

Unknown keys, wrong types, empty terminal elements, `refresh_seconds` outside
1 through 86400, and `attach_timeout_seconds` outside 1 through 3600 are
visible configuration errors. The command does not silently fall back after
reading a present but malformed file.

Terminal spawning uses a detached user scope when available and otherwise a
new session with closed standard streams. Niri focus is best-effort: if `niri`
is missing, its JSON cannot be interpreted, or no exact window matches, Tmux
Plus launches a terminal rather than failing the open. Interactive creation
without a path starts in the selected host user's home directory. The public
CLI rejects an explicit missing directory; it never inherits Niri or Rofi's
incidental current directory.

## Views and rows

The picker has leaf-only peer views:

```text
Tmux › All
Tmux › Local
Tmux › <remote host in Host Mesh order>
```

`All` is the mixed list across live hosts, ordered by the existing session
recency rules. `Local` follows, and remote scopes follow in the stable order
provided by Host Mesh. Availability, activity, and session age never reorder
the scope ring. Empty and unavailable authoritative hosts retain their scope;
the view is not removed merely because it has no current rows. With no remote
hosts, `All` and `Local` collapse to one concrete `Local` scope.

Every normal browsing row is a tmux session. Host rows are not an intermediate
chooser layer.

Session rows reserve two physical lines:

```text
rofi-tmux-plus
Desktop B · ~/code/rofi-tmux-plus · 2 windows · open here · activity 4m
```

In a concrete host scope, the redundant host label is omitted. The working
directory is shortened for display only. Search metadata retains the logical
host ID, display label, complete path, session name, current window, and
status. Selection identity always comes from typed JSON in `ROFI_INFO`, never
from visible text.

The status vocabulary is:

- `open here`: a matching Niri terminal is visible on the current desktop;
- `attached`: tmux reports one or more clients but none can be focused here;
- `detached`: tmux reports no clients; and
- `unavailable`: the row is a retained snapshot from an unreachable host.

Attachment count is authoritative only for a live observation. An unavailable
row says when it was last seen and does not present its old attachment status
as current fact.

## Navigation

Browsing follows the suite-wide Rofi contract:

| Key | Behavior |
| --- | --- |
| Tab / Shift+Tab | Move to the next or previous row |
| Left / Right | Wrap through the `All`, `Local`, and remote scopes |
| Enter | Open the selected session |
| Escape | Close Rofi through its native cancel action |
| Ctrl+G | Close Rofi through the same native cancel action |
| Alt+R | Perform a bounded refresh |
| Typed name + Ctrl+Enter | Create or open on a concrete host scope |
| F2 | Begin renaming the selected session; Ctrl+Enter commits |
| Shift+Delete | Enter kill confirmation for the selected session |

Ctrl+B and Ctrl+F replace the text cursor actions displaced by Left and Right.
Rofi's default Ctrl+N remains row-down and is not reused for session creation.

The managed invocation assigns Alt+R, Right, Left, and F2 to script callbacks
1, 2, 3, and 4; Shift+Delete uses the delete-entry callback; and Ctrl+Enter
remains the custom-input binding. Escape and Ctrl+G are Rofi's native cancel
bindings and never enter the script callback path. `ROFI_RETV=2` therefore
means create/open while browsing and commit while in rename state. `ROFI_DATA`
carries typed scope and action state across callbacks.

Custom creation is enabled only in a concrete host scope. From `All`, Tmux
Plus renders `Choose a concrete host view before creating a session.` and does
not open a host chooser, guess a destination, or invoke lifecycle code.

The non-browsing states are explicit:

```text
concrete scope ──Ctrl+Enter name──> create/open
session ────────F2───────────────> rename input
session ────────Shift+Delete─────> kill confirmation
```

Left and Right do nothing in rename and confirmation states so a pending
operation cannot be changed accidentally. Native Escape closes the picker from
every state, discarding an uncommitted action. Rename input is submitted only
with Ctrl+Enter; plain Enter retains its browse meaning and does not
ambiguously select a row while editing. Rename and kill leave the picker open
and refresh the affected host. Opening or creating a session closes the picker
after focusing or launching the terminal. A selected session is handed
directly to the lifecycle service from its typed Rofi metadata. The picker
model is reloaded only when an open fails and the dialog must reconcile visible
state; the lifecycle service independently revalidates Mesh authority and the
full stable reference before acting.

Configuration, model, and callback failures are bounded at the Rofi process
boundary. Escape and Ctrl+G remain native cancel actions even when a
configuration, model, callback state, or companion contract is malformed. The
legacy callback number 15 is an immediate no-op for stale pre-P8 invocations;
it never renders state or performs an action. Arrow callback failures render a
bounded diagnostic while retaining a safe picker state.

Kill confirmation selects `Cancel` by default. Its destructive row names the
logical host and session and reports how many clients the live observation
would disconnect.

## Stable identity and action safety

The authoritative session reference is:

```text
(logical host ID, tmux server generation, tmux session ID, creation timestamp)
```

Tmux session names are display and creation inputs, not durable identities.
Every attach, rename, and kill re-reads the target through the selected host
and verifies the server generation, session ID, and creation timestamp in one
bounded operation. Rename and kill also require the observed name as an
optimistic-concurrency precondition. Open normally does not, so an external
rename of the same proven session remains openable. An identity mismatch
returns `stale_session` and refreshes rather than risking an action against a
different session after a tmux server restart.

Tmux targets use the session ID after validation. Remote command fragments and
all dynamic values are shell-quoted; local processes use argv arrays. User
options accepted for programmatic creation are restricted to tmux `@` session
options.

## Discovery and cache lifecycle

Opening the picker must not wait for every SSH host:

1. Query the local default tmux server synchronously.
2. Load the most recent valid remote snapshots.
3. Render immediately and start at most one detached remote refresh.
4. Pin that refresh to one Host Mesh revision and query configured remote hosts
   concurrently with bounded SSH attempts.
5. Use Rofi's timeout callback to replace rows while preserving the active
   filter and selection.
6. Stop polling and clear transient status after completion or timeout.

The private picker model exposes the complete current logical-host catalog in
Mesh declaration order separately from observed inventory rows. Thus the flat
scope ring can offer configured remotes on a cold cache without pretending
that they were already contacted.

A successful host refresh, including a reachable host with no tmux server or
no sessions, replaces that host's cached inventory. A transport failure
retains the previous snapshot and marks it unavailable; any retained client
count is cleared because it is no longer a current attachment observation. A
non-authoritative reached-domain error has the same retained/unavailable
presentation. A reachable host on which tmux is missing is a visible capability
error, not an SSH route failure, and authoritatively clears old sessions.

Remote cache files are private, versioned, fingerprinted by Mesh revision and
cache schema, locked during mutation, and atomically replaced. Presentation
snapshots use a separate private cache with 0700 directories, 0600 regular
files, content-addressed names, bounded payloads, and atomic writes; garbage
collection only considers owned regular files matching the exact snapshot-name
shape. Cache layout is private implementation state and is not an integration
contract. Refresh
markers are also revision-scoped: a marker from an old Mesh cannot block or
surface as the current refresh. The detached inventory owner has a 15-second
hard deadline; its marker becomes `stalled` only after 20 seconds, so a normal
bounded refresh is never labelled stalled before its deadline.

The live inventory operation defined by Tmux Session Contract v1 does not
return cached sessions. The picker and higher-level consumers decide whether
and how to retain stale domain data.

## SSH and remote requirements

Background discovery and noninteractive management require key-, agent-, or
equivalent noninteractive SSH authentication. They use `BatchMode=yes`, the
Host Mesh timeout policy, and no automatic host-key acceptance. Interactive
terminal attachment may still expose normal SSH diagnostics.

Consumers try the mesh's ordered route candidates using their actual domain
command, rather than performing a separate `ssh ... true` probe. Once a remote
nonce-bearing Host Mesh v1 marker establishes that the authenticated wrapper
ran, Tmux Plus reports that route as reachable even if tmux is absent or its
command fails. It reports a route unreachable only for a classified SSH
transport failure before the marker and includes the mesh revision and
observation time in every report.

Remote hosts require SSH, a POSIX-compatible shell for the bounded discovery
wrapper, and tmux. They do not require this repository or Python to be
installed.

## Open, create, rename, and kill

Open first revalidates the selected reference and any optional generic
`@NAME=VALUE` requirements. It then looks for a current Niri window matching
the live session name and native host identity. If found, it focuses that
window. Otherwise it launches the configured terminal in a detached user scope
and attaches by exact tmux session ID, locally or through `ssh -t`. A missing
or changed required option is `stale_session`, before focus or terminal launch;
the generic contract never attributes those options to a provider.

Window matching is best-effort. It benefits from a tmux title containing the
session name and native hostname, but failure never prevents a new client from
opening.

Interactive creation by name behaves as ensure-and-open: an exact existing
name is opened; otherwise a new default-shell session is created and opened.
The default directory is the selected host user's home.
The public automation contract exposes stricter `create` semantics that fail
on a name collision, allowing Agent Plus to preserve provider ownership and
choose another name safely. Its provider-neutral defer-until-attached option
preserves waiting-wrapper reuse without teaching Tmux Plus about agent types.

Rename and kill act on a revalidated stable reference. Creation can atomically
set requested `@` session options and install a bounded first-attachment wait
gate before returning success. A holding wrapper prevents the requested
program from exiting before metadata and the complete descriptor are secured.
If creation or metadata setup fails, cleanup requires the operation token and
full stable identity; an unrelated or externally replaced session is never
removed.

## Process ownership and errors

Rofi callbacks never wait for a terminal process. Terminal windows and remote
attachments are detached from Rofi and survive picker exit. Background
refresh has one lock-protected owner and a hard deadline.

Action failures keep the picker open with a short, self-clearing message.
Network errors are summarized per logical host and bounded in length. Command
exit status is kept distinct from displayed diagnostics.

## Non-goals

- Managing tmux windows or panes interactively.
- Alternate tmux sockets or servers.
- Zellij or another multiplexer.
- Discovering arbitrary SSH configuration or `known_hosts`.
- Moving or synchronizing sessions between hosts.
- Provider-specific agent discovery or resume policy.
- Replacing the tmux-native interface after attachment.
- A resident daemon, compiled Rofi plugin, or DMS integration in this repo.

## Implementation sequence

1. Implement and test the Tmux Session v1 model and local inventory CLI.
2. Publish success, partial-host, stale-mesh, stale-session, and creation
   rollback fixtures for contract consumers.
3. Add stable local lifecycle operations and isolated tmux integration tests.
4. Consume Host Mesh v1 for bounded remote inventory and route reporting.
5. Implement Rofi rows, views, action states, and regression tests.
6. Integrate Agent Plus only after the contract passes independently.
7. The managed Chezmoi source installs all public commands on `PATH`, keeps
   raw Ghostty on `Mod+T`, adds `Mod+Return` as a second terminal shortcut,
   cuts `Mod+G` over to Tmux Plus, and retains the tmux cheatsheet on
   `Mod+Shift+G`. P6 live focus, attach, and remote acceptance completed for
   the exercised local and remote paths; those remain host-specific rollout
   checks for later changes.
