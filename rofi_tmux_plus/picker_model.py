"""Private retained-remote picker model and bounded detached refresh owner."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ContractError, clean_message
from .host import LocalHost, local_host
from .inventory_service import InventoryService
from .lifecycle import LocalLifecycle, now_millis
from .mesh_adapter import HostMeshAdapter, MeshSnapshot, MeshStaleError
from .remote_cache import CacheState, RemoteCache
from .tmux import TmuxClient
from .viewer_cache import LOCAL_ONLY_REVISION, ViewerObservationCache

# InventoryService gives the detached owner a 15-second whole-operation
# deadline.  A marker only becomes stalled after that deadline plus a small
# scheduling margin, so a normal bounded refresh is never labelled stalled
# while it still has time to finish.
_REFRESH_HARD_DEADLINE_SECONDS = 15.0
_REFRESH_STALL_SECONDS = 20


def detached_refresh_command(
    revision: str,
    *,
    package_file: str | Path = __file__,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Resolve a self-contained private refresh entry point.

    A source checkout or external-tree symlink keeps its nearby ``bin``
    launcher; an installed package uses its console script.  Do not rely on a
    child interpreter inheriting this process's import path.
    """
    launcher = Path(package_file).resolve().parents[1] / "bin" / "rofi-tmux-plus"
    if launcher.is_file():
        return [sys.executable, str(launcher), "_refresh", "--mesh-revision", revision]
    installed = which("rofi-tmux-plus")
    if installed is not None:
        return [installed, "_refresh", "--mesh-revision", revision]
    raise OSError("cannot resolve rofi-tmux-plus refresh executable")


@dataclass(frozen=True, slots=True)
class PickerModel:
    payload: dict[str, object]
    refresh_needed: bool


class RemoteRefresh:
    """Single-owner refresh operation; it has no resident background thread."""

    def __init__(
        self,
        config: Config,
        cache: RemoteCache,
        *,
        viewer_cache: ViewerObservationCache | None = None,
        mesh_adapter: HostMeshAdapter | None = None,
        inventory_factory: Callable[..., InventoryService] = InventoryService,
        process_starter: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._viewer_cache = viewer_cache or ViewerObservationCache(cache.directory)
        self._adapter = mesh_adapter or HostMeshAdapter()
        self._inventory_factory = inventory_factory
        self._process_starter = process_starter or self._spawn

    @staticmethod
    def _spawn(argv: Sequence[str]) -> None:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def request(self, snapshot: MeshSnapshot | None, *, force: bool = False) -> bool:
        """Ask a short-lived child to refresh; the child lock elects the owner."""
        revision = snapshot.revision if snapshot is not None else LOCAL_ONLY_REVISION
        try:
            marker = self._cache.marker(
                mesh_revision=revision,
                stall_after_seconds=_REFRESH_STALL_SECONDS,
            )
            if marker is not None and marker["state"] == "running":
                return False
            if (
                not force
                and marker is not None
                and marker.get("state") in {"complete", "failed", "stale", "stalled"}
            ):
                updated_at = marker.get("updatedAt")
                if (
                    isinstance(updated_at, int)
                    and not isinstance(updated_at, bool)
                    and 0 <= self._cache.now_millis() - updated_at < 10_000
                ):
                    return False
            # Mark the finite owner before spawning so repeated timeout or
            # cursor callbacks cannot launch duplicate helpers in the gap
            # before the child acquires its lock.
            self._cache.write_marker("running", revision)
            self._process_starter(detached_refresh_command(revision))
        except Exception as error:  # noqa: BLE001 - private detached-process boundary
            # A failed spawn must not make the synchronous local picker model
            # fail.  Persist the bounded state when possible for the future UI.
            try:
                self._cache.write_marker("failed", revision, message=clean_message(error))
            except OSError:
                pass
            return False
        return True

    def run(self, revision: str) -> bool:
        """Run one revision-pinned owner and viewer refresh without a resident worker."""
        with self._cache.lock(refresh=True, blocking=False) as acquired:
            if not acquired:
                return False
            self._cache.write_marker("running", revision)
            deadline = time.monotonic() + _REFRESH_HARD_DEADLINE_SECONDS
            try:
                snapshot = self._adapter.load()
                local_only = revision == LOCAL_ONLY_REVISION
                if (local_only and snapshot is not None) or (
                    not local_only and (snapshot is None or snapshot.revision != revision)
                ):
                    self._cache.write_marker("stale", revision, message="the Host Mesh changed")
                    return True
                current_revision = None if local_only else revision
                host_ids = [] if snapshot is None else [host.host_id for host in snapshot.hosts]
                endpoint_host_id = (
                    local_host().host_id if snapshot is None else snapshot.local_host.host_id
                )
                expected_context = self._viewer_cache.context_id()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ContractError("operation_failed", "picker refresh deadline exceeded")
                service = self._inventory_factory(
                    self._config,
                    mesh_adapter=self._adapter,
                    # The preflight Mesh observation has a bounded process
                    # call too.  Give the live inventory only the remainder
                    # of this owner's hard deadline.
                    whole_deadline_seconds=remaining,
                )
                response = service.inventory(
                    requested_hosts=host_ids,
                    mesh_revision=current_revision,
                    panes=False,
                    option_names=(),
                    with_viewers=True,
                )
                expected_host_ids = (
                    [local_host().host_id]
                    if snapshot is None
                    else [host.host_id for host in snapshot.hosts]
                )
                response_hosts = response.get("hosts") if isinstance(response, dict) else None
                if (
                    not isinstance(response, dict)
                    or response.get("schemaVersion") != 1
                    or response.get("meshRevision") != current_revision
                    or not isinstance(response.get("generatedAt"), int)
                    or isinstance(response.get("generatedAt"), bool)
                    or response["generatedAt"] < 0
                    or not isinstance(response.get("hosts"), list)
                    or not isinstance(response.get("viewerEndpoint"), dict)
                    or not isinstance(response_hosts, list)
                    or len(response_hosts) != len(expected_host_ids)
                    or any(not isinstance(row, dict) for row in response_hosts)
                    or {row.get("hostId") for row in response_hosts if isinstance(row, dict)}
                    != set(expected_host_ids)
                ):
                    raise ContractError(
                        "operation_failed", "picker refresh returned an invalid inventory"
                    )
                current_snapshot = self._adapter.load()
                if (local_only and current_snapshot is not None) or (
                    not local_only
                    and (current_snapshot is None or current_snapshot.revision != revision)
                ):
                    self._cache.write_marker("stale", revision, message="the Host Mesh changed")
                    return True
                rows = response_hosts
                if snapshot is not None:
                    remote_ids = {host.host_id for host in snapshot.hosts if not host.local}
                    remote_rows = [
                        {
                            **row,
                            "sessions": [
                                {
                                    key: value
                                    for key, value in session.items()
                                    if key != "localViewer"
                                }
                                if isinstance(session, dict)
                                else session
                                for session in row.get("sessions", [])
                            ],
                        }
                        for row in rows
                        if isinstance(row, dict) and row.get("hostId") in remote_ids
                    ]
                    self._cache.merge(snapshot, remote_rows)
                # A desktop-context change discards only viewer observations;
                # current owner facts remain useful in the retained-remote cache.
                self._viewer_cache.merge_response(
                    response,
                    mesh_revision=current_revision,
                    expected_context=expected_context,
                    endpoint_host_id=endpoint_host_id,
                    expected_host_ids=expected_host_ids,
                )
            except MeshStaleError:
                self._cache.write_marker("stale", revision, message="the Host Mesh changed")
            except ContractError as error:
                self._cache.write_marker("failed", revision, message=error.message)
            except Exception as error:  # noqa: BLE001 - private child process boundary
                self._cache.write_marker("failed", revision, message=clean_message(error))
            else:
                self._cache.write_marker("complete", revision)
            return True

    def status(self, mesh_revision: str) -> dict[str, object] | None:
        return self._cache.marker(
            mesh_revision=mesh_revision, stall_after_seconds=_REFRESH_STALL_SECONDS
        )


class PickerModelService:
    """Synchronous local inventory plus retained remote rows, never live SSH."""

    def __init__(
        self,
        config: Config,
        *,
        cache: RemoteCache | None = None,
        viewer_cache: ViewerObservationCache | None = None,
        mesh_adapter: HostMeshAdapter | None = None,
        local_tmux: TmuxClient | None = None,
        refresher: RemoteRefresh | None = None,
        inventory_factory: Callable[..., InventoryService] = InventoryService,
        now: Callable[[], int] = now_millis,
    ) -> None:
        self._config = config
        self._cache = cache or RemoteCache()
        self._viewer_cache = viewer_cache or ViewerObservationCache(self._cache.directory)
        self._adapter = mesh_adapter or HostMeshAdapter()
        self._local_tmux = local_tmux or TmuxClient()
        self._refresher = refresher or RemoteRefresh(
            config,
            self._cache,
            viewer_cache=self._viewer_cache,
            mesh_adapter=self._adapter,
        )
        self._inventory_factory = inventory_factory
        self._now = now

    @staticmethod
    def _local(snapshot: MeshSnapshot | None, tmux: TmuxClient, config: Config) -> LocalLifecycle:
        fallback = local_host()
        if snapshot is None:
            return LocalLifecycle(tmux, config, host=fallback)
        host = snapshot.local_host
        return LocalLifecycle(
            tmux,
            config,
            host=LocalHost(
                host.host_id,
                host.display,
                fallback.native_hostname,
                frozenset({host.host_id, *host.aliases}),
            ),
        )

    @staticmethod
    def _catalog(snapshot: MeshSnapshot | None, local: LocalLifecycle) -> list[dict[str, object]]:
        if snapshot is None:
            return [
                {
                    "hostId": local.host.host_id,
                    "display": local.host.display,
                    "local": True,
                }
            ]
        return [
            {"hostId": host.host_id, "display": host.display, "local": host.local}
            for host in snapshot.hosts
        ]

    def _remote_refresh_needed(self, snapshot: MeshSnapshot, state: CacheState | None) -> bool:
        remote_hosts = [host for host in snapshot.hosts if not host.local]
        if not remote_hosts:
            return False
        if not isinstance(state, CacheState):
            return True
        cached = {str(row["hostId"]) for row in state.hosts}
        configured = {host.host_id for host in remote_hosts}
        return cached != configured or (
            self._now() - state.written_at >= self._config.refresh_seconds * 1000
        )

    def _snapshot_payload(
        self,
        snapshot: MeshSnapshot,
        local: LocalLifecycle,
        local_row: dict[str, object],
        state: CacheState | None,
        *,
        requested: bool = False,
    ) -> tuple[dict[str, object], bool]:
        remote_rows = [] if state is None else list(state.hosts)
        refresh_needed = self._remote_refresh_needed(snapshot, state)
        return (
            {
                "schemaVersion": 1,
                "generatedAt": self._now(),
                "meshRevision": snapshot.revision,
                "hosts": [local_row, *remote_rows],
                "hostCatalog": self._catalog(snapshot, local),
                "remoteRefreshNeeded": refresh_needed,
                "remoteRefreshRequested": requested,
                "remoteRefresh": self._refresher.status(snapshot.revision),
            },
            refresh_needed,
        )

    def _decorate_viewers(
        self, payload: dict[str, object], mesh_revision: str | None, endpoint_host_id: str
    ) -> bool:
        needed = self._viewer_cache.decorate(
            payload,
            mesh_revision=mesh_revision,
            endpoint_host_id=endpoint_host_id,
        )
        payload["viewerRefreshNeeded"] = needed
        return needed

    def load(self, *, start_refresh: bool = True) -> PickerModel:
        snapshot = self._adapter.load()
        local = self._local(snapshot, self._local_tmux, self._config)
        if snapshot is None:
            row = local.inventory(local.host.host_id, panes=False, option_names=())
            payload: dict[str, object] = {
                "schemaVersion": 1,
                "generatedAt": self._now(),
                "meshRevision": None,
                "hosts": [row],
                "hostCatalog": self._catalog(None, local),
                "remoteRefreshNeeded": False,
                "remoteRefreshRequested": False,
                "remoteRefresh": self._refresher.status(LOCAL_ONLY_REVISION),
            }
            viewer_needed = self._decorate_viewers(payload, None, local.host.host_id)
            requested = self._refresher.request(None) if start_refresh and viewer_needed else False
            payload["remoteRefreshRequested"] = requested
            payload["remoteRefresh"] = self._refresher.status(LOCAL_ONLY_REVISION)
            return PickerModel(payload, viewer_needed)
        local_row = local.inventory(snapshot.local_host.host_id, panes=False, option_names=())
        state = self._cache.load(snapshot)
        payload, remote_needed = self._snapshot_payload(snapshot, local, local_row, state)
        viewer_needed = self._decorate_viewers(
            payload, snapshot.revision, snapshot.local_host.host_id
        )
        refresh_needed = remote_needed or viewer_needed
        requested = self._refresher.request(snapshot) if start_refresh and refresh_needed else False
        payload["remoteRefreshRequested"] = requested
        payload["remoteRefresh"] = self._refresher.status(snapshot.revision)
        return PickerModel(payload, refresh_needed)

    def refresh_now(self) -> PickerModel:
        """Request one finite detached owner/viewer refresh, then reload cache."""
        snapshot = self._adapter.load()
        self._refresher.request(snapshot, force=True)
        return self.load(start_refresh=False)

    def refresh_host(self, host_id: str, mesh_revision: str | None) -> PickerModel:
        """Synchronously reconcile one affected host after a mutation.

        This intentionally never requests a detached all-host refresh. The
        local row is always read live; a remote row is revision-pinned and
        atomically merged with retained peers in the private cache.
        """
        snapshot = self._adapter.load()
        local = self._local(snapshot, self._local_tmux, self._config)
        if snapshot is None:
            if mesh_revision is not None or host_id.casefold() != local.host.host_id.casefold():
                raise ContractError(
                    "stale_mesh", "the selected host mesh changed; refresh and try again"
                )
            row = local.inventory(local.host.host_id, panes=False, option_names=())
            payload: dict[str, object] = {
                "schemaVersion": 1,
                "generatedAt": self._now(),
                "meshRevision": None,
                "hosts": [row],
                "hostCatalog": self._catalog(None, local),
                "remoteRefreshNeeded": False,
                "remoteRefreshRequested": False,
                "remoteRefresh": self._refresher.status(LOCAL_ONLY_REVISION),
            }
            viewer_needed = self._decorate_viewers(payload, None, local.host.host_id)
            return PickerModel(payload, viewer_needed)
        if mesh_revision != snapshot.revision:
            raise ContractError(
                "stale_mesh", "the selected host mesh changed; refresh and try again"
            )
        selected = snapshot.resolve_host(host_id)
        local_row = local.inventory(snapshot.local_host.host_id, panes=False, option_names=())
        if selected.local:
            payload, refresh_needed = self._snapshot_payload(
                snapshot, local, local_row, self._cache.load(snapshot)
            )
            viewer_needed = self._decorate_viewers(
                payload, snapshot.revision, snapshot.local_host.host_id
            )
            return PickerModel(payload, refresh_needed or viewer_needed)
        service = self._inventory_factory(
            self._config,
            mesh_adapter=self._adapter,
            local_tmux=self._local_tmux,
        )
        response = service.inventory(
            requested_hosts=[selected.host_id],
            mesh_revision=snapshot.revision,
            panes=False,
            option_names=(),
        )
        if (
            not isinstance(response, dict)
            or response.get("schemaVersion") != 1
            or response.get("meshRevision") != snapshot.revision
            or not isinstance(response.get("hosts"), list)
            or len(response["hosts"]) != 1
            or not isinstance(response["hosts"][0], dict)
        ):
            raise ContractError(
                "operation_failed", "affected-host refresh returned an invalid inventory"
            )
        state = self._cache.merge_host(snapshot, selected.host_id, response["hosts"][0])
        payload, refresh_needed = self._snapshot_payload(snapshot, local, local_row, state)
        viewer_needed = self._decorate_viewers(
            payload, snapshot.revision, snapshot.local_host.host_id
        )
        return PickerModel(payload, refresh_needed or viewer_needed)

    def refresh_host_current(self, host_id: str) -> PickerModel:
        """Refresh one host against the currently loaded Mesh revision.

        Used only after an already-completed mutation whose old revision has
        become stale.  It is never an authority for the mutation itself.
        """
        snapshot = self._adapter.load()
        return self.refresh_host(host_id, None if snapshot is None else snapshot.revision)
