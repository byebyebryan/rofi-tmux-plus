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
from .diagnostics import timed
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


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError("operation_failed", "picker refresh deadline exceeded")
    return remaining


def _load_mesh(adapter: HostMeshAdapter, deadline: float) -> MeshSnapshot | None:
    return adapter.load(timeout_seconds=min(5, _remaining(deadline)))


def detached_refresh_command(
    revision: str,
    *,
    package_file: str | Path = __file__,
    which: Callable[[str], str | None] = shutil.which,
    kind: str = "owner",
) -> list[str]:
    """Resolve a self-contained private refresh entry point.

    A source checkout or external-tree symlink keeps its nearby ``bin``
    launcher; an installed package uses its console script.  Do not rely on a
    child interpreter inheriting this process's import path.
    """
    launcher = Path(package_file).resolve().parents[1] / "bin" / "rofi-tmux-plus"
    if launcher.is_file():
        command = [sys.executable, str(launcher), "_refresh", "--mesh-revision", revision]
        return command + (["--kind", kind] if kind != "owner" else [])
    installed = which("rofi-tmux-plus")
    if installed is not None:
        command = [installed, "_refresh", "--mesh-revision", revision]
        return command + (["--kind", kind] if kind != "owner" else [])
    raise OSError("cannot resolve rofi-tmux-plus refresh executable")


@dataclass(frozen=True, slots=True)
class PickerModel:
    payload: dict[str, object]
    refresh_needed: bool


class RemoteRefresh:
    """Single-owner refresh operation; it has no resident background thread."""

    job_kind = "owner"

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
        try:
            with self._cache.lock(request=True, blocking=False) as acquired:
                if not acquired:
                    return False
                return self._request_locked(snapshot, force=force)
        except OSError:
            return False

    def _request_locked(self, snapshot: MeshSnapshot | None, *, force: bool) -> bool:
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
                    and 0
                    <= self._cache.now_millis() - updated_at
                    < (
                        10_000
                        if marker.get("state") != "complete"
                        else 7_000
                        if self.job_kind == "viewer"
                        else 1_000
                    )
                ):
                    return False
            # Mark the finite owner before spawning so repeated timeout or
            # cursor callbacks cannot launch duplicate helpers in the gap
            # before the child acquires its lock.
            self._cache.write_marker("running", revision)
            self._process_starter(detached_refresh_command(revision, kind=self.job_kind))
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
        """Collect owners only, publishing each completed peer before the batch ends."""
        with self._cache.lock(refresh=True, blocking=False) as acquired:
            if not acquired:
                return False
            self._cache.write_marker("running", revision)
            deadline = time.monotonic() + _REFRESH_HARD_DEADLINE_SECONDS
            try:
                snapshot = _load_mesh(self._adapter, deadline)
                if snapshot is None:
                    if revision != LOCAL_ONLY_REVISION:
                        raise MeshStaleError()
                    self._cache.write_marker("complete", revision)
                    return True
                if snapshot.revision != revision:
                    raise MeshStaleError()
                host_ids = [host.host_id for host in snapshot.hosts if not host.local]
                if not host_ids:
                    self._cache.write_marker("complete", revision)
                    return True
                tokens = self._cache.reserve(snapshot, host_ids)
                published: set[str] = set()

                def publish(current: MeshSnapshot, host, row: dict[str, object]) -> None:
                    latest = _load_mesh(self._adapter, deadline)
                    if (
                        latest is None
                        or latest.revision != revision
                        or current.revision != revision
                    ):
                        raise MeshStaleError()
                    checked = dict(row)
                    checked["sessions"] = [
                        {key: value for key, value in session.items() if key != "localViewer"}
                        for session in row.get("sessions", [])
                    ]
                    self._cache.merge_host(
                        snapshot, host.host_id, checked, operation_token=tokens[host.host_id]
                    )
                    published.add(host.host_id)

                remaining = _remaining(deadline)
                service = self._inventory_factory(
                    self._config,
                    mesh_adapter=self._adapter,
                    whole_deadline_seconds=remaining,
                    on_host=publish,
                    snapshot=snapshot,
                )
                response = service.inventory(
                    requested_hosts=host_ids,
                    mesh_revision=revision,
                    panes=False,
                    option_names=(),
                    with_viewers=False,
                )
                rows = response.get("hosts") if isinstance(response, dict) else None
                if (
                    not isinstance(response, dict)
                    or type(response.get("schemaVersion")) is not int
                    or response["schemaVersion"] != 1
                    or response.get("meshRevision") != revision
                    or type(response.get("generatedAt")) is not int
                    or response["generatedAt"] < 0
                    or not isinstance(rows, list)
                ):
                    raise ContractError(
                        "operation_failed", "picker refresh returned an invalid inventory"
                    )
                # The public aggregate still validates as one complete response.
                stripped = [
                    {
                        **row,
                        "sessions": [
                            {key: value for key, value in session.items() if key != "localViewer"}
                            for session in row.get("sessions", [])
                        ],
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
                self._cache._current_rows(snapshot, stripped)
                for row in rows:
                    if row["hostId"] not in published:
                        publish(snapshot, snapshot.resolve_host(row["hostId"]), row)
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


class _ViewerJobCache(RemoteCache):
    @property
    def _request_lock_path(self) -> Path:
        return self.directory / "viewer-request-v2.lock"

    @property
    def _refresh_lock_path(self) -> Path:
        return self.directory / "viewer-refresh-v1.lock"

    @property
    def _marker_path(self) -> Path:
        return self.directory / "viewer-refresh-v1.json"


class ViewerRefresh(RemoteRefresh):
    """Finite endpoint scan using retained owner facts; it never inventories SSH peers."""

    job_kind = "viewer"

    def __init__(self, config: Config, cache: RemoteCache, **kwargs) -> None:
        self._owner_cache = cache
        super().__init__(
            config,
            _ViewerJobCache(cache.directory, now_millis=cache.now_millis),
            **kwargs,
        )

    def run(self, revision: str) -> bool:
        with self._cache.lock(refresh=True, blocking=False) as acquired:
            if not acquired:
                return False
            self._cache.write_marker("running", revision)
            deadline = time.monotonic() + _REFRESH_HARD_DEADLINE_SECONDS
            try:
                snapshot = _load_mesh(self._adapter, deadline)
                current_revision = snapshot.revision if snapshot else LOCAL_ONLY_REVISION
                if current_revision != revision:
                    raise MeshStaleError()
                context = self._viewer_cache.context_id()
                model = (
                    PickerModelService(
                        self._config,
                        cache=self._owner_cache,
                        viewer_cache=self._viewer_cache,
                        mesh_adapter=self._adapter,
                    )
                    ._load_snapshot(snapshot, start_refresh=False)
                    .payload
                )
                response = {
                    "schemaVersion": 1,
                    "generatedAt": model["generatedAt"],
                    "meshRevision": model["meshRevision"],
                    "hosts": model["hosts"],
                }
                endpoint = model["viewerEndpointHostId"]
                service = self._inventory_factory(self._config, mesh_adapter=self._adapter)
                service._enrich_local_viewers(
                    response,
                    endpoint,
                    snapshot.policy.executable if snapshot else None,
                    deadline=min(deadline, time.monotonic() + 3.0),
                )
                latest = _load_mesh(self._adapter, deadline)
                if (latest.revision if latest else LOCAL_ONLY_REVISION) != revision:
                    raise MeshStaleError()
                if not self._viewer_cache.merge_response(
                    response,
                    mesh_revision=model["meshRevision"],
                    expected_context=context,
                    endpoint_host_id=endpoint,
                    expected_host_ids=[row["hostId"] for row in response["hosts"]],
                ):
                    raise ContractError(
                        "operation_failed", "viewer context changed during observation"
                    )
            except MeshStaleError:
                self._cache.write_marker("stale", revision, message="the Host Mesh changed")
            except Exception as error:  # noqa: BLE001 - finite observation process boundary
                self._cache.write_marker("failed", revision, message=clean_message(error))
            else:
                self._cache.write_marker("complete", revision)
            return True


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
        viewer_refresher: ViewerRefresh | None = None,
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
        self._viewer_refresher = viewer_refresher or ViewerRefresh(
            config, self._cache, viewer_cache=self._viewer_cache, mesh_adapter=self._adapter
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
        renewal = (self._config.refresh_seconds - min(3, self._config.refresh_seconds / 3)) * 1000
        return cached != configured or any(
            not 0
            <= self._now() - row.get("lastAttemptAt", row["observedAt"])
            < (10_000 if row["status"] in {"error", "unreachable"} else renewal)
            for row in state.hosts
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
                "ownerFreshnessSeconds": self._config.refresh_seconds,
            },
            refresh_needed,
        )

    def _decorate_viewers(
        self, payload: dict[str, object], mesh_revision: str | None, endpoint_host_id: str
    ) -> bool:
        payload["ownerFreshnessSeconds"] = self._config.refresh_seconds
        for row in payload["hosts"]:
            if not row.get("local"):
                observed = row.get("ownerObservedAt", row.get("observedAt"))
                if (
                    type(observed) is not int
                    or not 0 <= self._now() - observed < self._config.refresh_seconds * 1000
                ):
                    for session in row["sessions"]:
                        session["attachedClients"] = None
                    row["ownerFactsExpired"] = True
        needed = self._viewer_cache.decorate(
            payload,
            mesh_revision=mesh_revision,
            endpoint_host_id=endpoint_host_id,
        )
        payload["viewerRefreshNeeded"] = needed
        return needed

    def _next_refresh(self, payload: dict[str, object]) -> None:
        now = self._now()
        deadlines: list[float] = []
        rows = {row["hostId"]: row for row in payload["hosts"]}
        for host in payload["hostCatalog"]:
            if host["local"]:
                continue
            row = rows.get(host["hostId"])
            if row is None:
                deadlines.append(now + 1000)
                continue
            interval = (
                10_000
                if row["status"] in {"error", "unreachable"}
                else (self._config.refresh_seconds - min(3, self._config.refresh_seconds / 3))
                * 1000
            )
            observed = row.get("lastAttemptAt", row["observedAt"])
            deadlines.append(observed + interval if observed <= now else now + 1000)
        if any(row["sessions"] for row in payload["hosts"]):
            observed = payload.get("viewerObservedAt")
            positive = any(
                session.get("localViewer", {}).get("state") == "open"
                for row in payload["hosts"]
                for session in row["sessions"]
            )
            interval = 7000 if positive else 10_000
            deadlines.append(
                observed + interval if type(observed) is int and observed <= now else now + 1000
            )
        payload["nextRefreshAt"] = max(now + 1000, min(deadlines)) if deadlines else None

    def _request_viewer(self, snapshot: MeshSnapshot | None, payload: dict[str, object]) -> bool:
        revision = snapshot.revision if snapshot is not None else LOCAL_ONLY_REVISION
        marker = self._viewer_refresher.status(revision)
        # New owner references should not wait for a completed scan's normal
        # cooldown. Failed scans still retain their retry budget.
        changed = payload.get("viewerInputsChanged") is True and (
            isinstance(marker, dict) and marker.get("state") == "complete"
        )
        return (
            self._viewer_refresher.request(snapshot, force=True)
            if changed
            else self._viewer_refresher.request(snapshot)
        )

    @timed("picker_model")
    def load(self, *, start_refresh: bool = True, deadline: float | None = None) -> PickerModel:
        snapshot = self._adapter.load() if deadline is None else _load_mesh(self._adapter, deadline)
        return self._load_snapshot(snapshot, start_refresh=start_refresh)

    def _load_snapshot(self, snapshot: MeshSnapshot | None, *, start_refresh: bool) -> PickerModel:
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
            requested = (
                self._request_viewer(None, payload) if start_refresh and viewer_needed else False
            )
            payload["viewerRefreshRequested"] = requested
            payload["viewerRefresh"] = self._viewer_refresher.status(LOCAL_ONLY_REVISION)
            payload["remoteRefresh"] = self._refresher.status(LOCAL_ONLY_REVISION)
            self._next_refresh(payload)
            return PickerModel(payload, viewer_needed)
        local_row = local.inventory(snapshot.local_host.host_id, panes=False, option_names=())
        state = self._cache.load(snapshot)
        payload, remote_needed = self._snapshot_payload(snapshot, local, local_row, state)
        viewer_needed = self._decorate_viewers(
            payload, snapshot.revision, snapshot.local_host.host_id
        )
        refresh_needed = remote_needed or viewer_needed
        requested = self._refresher.request(snapshot) if start_refresh and remote_needed else False
        viewer_requested = (
            self._request_viewer(snapshot, payload) if start_refresh and viewer_needed else False
        )
        payload["remoteRefreshRequested"] = requested
        payload["remoteRefresh"] = self._refresher.status(snapshot.revision)
        payload["viewerRefreshRequested"] = viewer_requested
        payload["viewerRefresh"] = self._viewer_refresher.status(snapshot.revision)
        self._next_refresh(payload)
        return PickerModel(payload, refresh_needed)

    def refresh_now(self) -> PickerModel:
        """Request one finite detached owner/viewer refresh, then reload cache."""
        snapshot = self._adapter.load()
        self._refresher.request(snapshot, force=True)
        self._viewer_refresher.request(snapshot, force=True)
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
        token = self._cache.reserve(snapshot, [selected.host_id])[selected.host_id]
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
        latest = self._adapter.load()
        if latest is None or latest.revision != snapshot.revision:
            raise MeshStaleError()
        state = self._cache.merge_host(
            snapshot, selected.host_id, response["hosts"][0], operation_token=token
        )
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
