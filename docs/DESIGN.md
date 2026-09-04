# Design: rofi-tmux-plus

Status: local and Host Mesh-backed remote lifecycle and live inventory, the
private retained remote cache and refresh lifecycle, and the complete Rofi
browse/open/create/rename/kill UI are implemented.

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

The initial picker has two top-level views:

```text
Tmux › Recent
Tmux › Hosts
Tmux › Hosts › Desktop B
```

`Recent` is a mixed list across live hosts ordered by tmux session activity.
Live sessions precede retained stale sessions. `Hosts` puts the local host
first and remote hosts in configured mesh order. Entering a host shows its
sessions.

There is intentionally no third view in version 1. Attachment status and
alphabetical ordering do not yet justify another navigation surface.

Session rows reserve two physical lines:

```text
rofi-tmux-plus
Desktop B · ~/code/rofi-tmux-plus · 2 windows · open here · activity 4m
```

Inside a host layer, the redundant host label is omitted. The working
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
| Left / Right | Switch `Recent` and `Hosts`, returning to a view root |
| Enter | Enter a host or open a session |
| Escape | Return one layer; exit from a browsing root |
| Ctrl+G | Exit unconditionally |
| Alt+R | Perform a bounded refresh |
| Typed name + Ctrl+Enter | Create or open a named session |
| F2 | Begin renaming the selected session; Ctrl+Enter commits |
| Shift+Delete | Enter kill confirmation for the selected session |

Ctrl+B and Ctrl+F replace the text cursor actions displaced by Left and Right.
Rofi's default Ctrl+N remains row-down and is not reused for session creation.

The managed invocation assigns Alt+R, Right, Left, F2, and Escape to script
callbacks 1, 2, 3, 4, and 6; Shift+Delete uses the delete-entry callback;
Ctrl+G remains Rofi's unconditional cancel binding; and Ctrl+Enter remains the
custom-input binding. `ROFI_RETV=2` therefore means create/open while browsing
and commit while in rename state. `ROFI_DATA` carries the typed state and
originating view across callbacks.

Custom creation is enabled in `Recent` and in a host's session list. From
`Recent`, the typed name is carried into a host chooser. Inside a host, it is
created or opened on that host. Custom input is disabled at the `Hosts` root;
the user enters a host first.

The non-browsing states are explicit:

```text
browse root ──Enter host──────> host sessions
      │                              │
      └─Ctrl+Enter name─> choose host│
                                     └─Ctrl+Enter name─> create/open

session ──F2────────────> rename input
session ──Shift+Delete──> kill confirmation
```

Left and Right do nothing in choose-host, rename, and confirmation states so a
pending operation cannot be discarded accidentally. Escape cancels to the
originating list. Rename input is submitted only with Ctrl+Enter; plain Enter
retains its browse meaning and does not ambiguously select a row while editing.
Rename and kill leave the picker open and refresh the affected host. Opening or
creating a session closes the picker after focusing or launching the terminal.

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
Mesh declaration order separately from observed inventory rows. Thus a Host or
create chooser can offer configured remotes on a cold cache without pretending
that they were already contacted.

A successful host refresh, including a reachable host with no tmux server or
no sessions, replaces that host's cached inventory. A transport failure
retains the previous snapshot and marks it unavailable; any retained client
count is cleared because it is no longer a current attachment observation. A
non-authoritative reached-domain error has the same retained/unavailable
presentation. A reachable host on which tmux is missing is a visible capability
error, not an SSH route failure, and authoritatively clears old sessions.

Cache files are private, versioned, fingerprinted by Mesh revision and cache
schema, locked during mutation, and atomically replaced. Cache layout
is private implementation state and is not an integration contract. Refresh
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

Open first revalidates the selected reference. It then looks for a current
Niri window matching the live session name and native host identity. If found,
it focuses that window. Otherwise it launches the configured terminal in a
detached user scope and attaches by exact tmux session ID, locally or through
`ssh -t`.

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
7. In a separate chezmoi deployment, install all public commands on `PATH`,
   keep raw Ghostty on `Mod+T`, add
   `Mod+Return` as a second terminal shortcut, cut `Mod+G` over from the DMS
   mux to Tmux Plus, and retain the tmux cheatsheet on `Mod+Shift+G`.
