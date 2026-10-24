# Prepared Mesh selection and operation

The picker consumes the already running Observer prepared Fleet v1 service.
Observer composes native owner facts and endpoint-local desktop evidence;
Mesh provides configured authority and cached transport. Plus neither starts
upstream services nor collects native state while browsing.

## Configure the producer

Follow [Tmux Observer's Mesh guide](https://github.com/byebyebryan/tmux-observer/blob/main/docs/guides/mesh-integration.md)
for the fixed `tmux_default` source, explicit transport selection and independent
owner/fleet startup. [Service contexts](https://github.com/byebyebryan/tmux-observer/blob/main/docs/guides/service-contexts.md)
bind prepared reads to the intended desktop environment. Host scope is
endpoint-specific and directional; cached rows never add routes.

The Plus artifact checks its exact Observer version and complete module hashes.
Follow [development](../maintenance/development.md) for the pinned producer; do not substitute
an arbitrary checkout or regenerate the guard for a documentation change.

## Read, refresh and action paths

Cached browsing starts no tmux/SSH/desktop collection. An incompatible or missing
prepared reader is an explicit failure, with no direct fallback. Unknown,
warming and unavailable rows retain their semantics. Open/Attached presentation
does not grant verified action handles.

Alt+R requests bounded passive refresh through the separate Observer control
path. Public fresh inventory and explicit lifecycle/window actions independently
validate routes and exact native references. See [client boundaries](../reference/architecture.md)
and [Tmux Session v1](../reference/tmux-session-v1.md).

## Managed selection and evidence

The [Observer operations guide](https://github.com/byebyebryan/tmux-observer/blob/main/docs/maintenance/operations.md)
and [deployment manifest](https://github.com/byebyebryan/dotfiles/blob/main/deployments/tmux-observer.json)
own exact installed selections and paired rollback. Tmux Reader/bridge and
generic shared CLI can retain different Mesh versions. Optional view-feed and
semantic delta are not selected by support in source.
