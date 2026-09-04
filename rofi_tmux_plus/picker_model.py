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
        mesh_adapter: HostMeshAdapter | None = None,
        inventory_factory: Callable[..., InventoryService] = InventoryService,
        process_starter: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
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

    def request(self, snapshot: MeshSnapshot) -> bool:
        """Ask a short-lived child to refresh; the child lock elects the owner."""
        try:
            marker = self._cache.marker(
                mesh_revision=snapshot.revision,
                stall_after_seconds=_REFRESH_STALL_SECONDS,
            )
            if marker is not None and marker["state"] == "running":
                return False
            self._process_starter(detached_refresh_command(snapshot.revision))
        except Exception as error:  # noqa: BLE001 - private detached-process boundary
            # A failed spawn must not make the synchronous local picker model
            # fail.  Persist the bounded state when possible for the future UI.
            try:
                self._cache.write_marker("failed", snapshot.revision, message=clean_message(error))
            except OSError:
                pass
            return False
        return True

    def run(self, revision: str) -> bool:
        """Run one revision-pinned refresh, returning false if another owns it."""
        with self._cache.lock(refresh=True, blocking=False) as acquired:
            if not acquired:
                return False
            self._cache.write_marker("running", revision)
            deadline = time.monotonic() + _REFRESH_HARD_DEADLINE_SECONDS
            try:
                snapshot = self._adapter.load()
                if snapshot is None or snapshot.revision != revision:
                    self._cache.write_marker("stale", revision, message="the Host Mesh changed")
                    return True
                remote_ids = [host.host_id for host in snapshot.hosts if not host.local]
                if not remote_ids:
                    self._cache.merge(snapshot, [])
                    self._cache.write_marker("complete", revision)
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ContractError("operation_failed", "remote refresh deadline exceeded")
                service = self._inventory_factory(
                    self._config,
                    mesh_adapter=self._adapter,
                    # The preflight Mesh observation has a bounded process
                    # call too.  Give the live inventory only the remainder
                    # of this owner's hard deadline.
                    whole_deadline_seconds=remaining,
                )
                response = service.inventory(
                    requested_hosts=remote_ids,
                    mesh_revision=revision,
                    panes=False,
                    option_names=(),
                )
                if (
                    not isinstance(response, dict)
                    or response.get("schemaVersion") != 1
                    or not isinstance(response.get("generatedAt"), int)
                    or isinstance(response.get("generatedAt"), bool)
                    or response["generatedAt"] < 0
                ):
                    raise ContractError(
                        "operation_failed", "remote refresh returned an invalid inventory"
                    )
                if response.get("meshRevision") != revision:
                    self._cache.write_marker("stale", revision, message="the Host Mesh changed")
                    return True
                rows = response.get("hosts")
                if not isinstance(rows, list):
                    raise ContractError(
                        "operation_failed", "remote refresh returned an invalid inventory"
                    )
                self._cache.merge(snapshot, rows)
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
        mesh_adapter: HostMeshAdapter | None = None,
        local_tmux: TmuxClient | None = None,
        refresher: RemoteRefresh | None = None,
        inventory_factory: Callable[..., InventoryService] = InventoryService,
        now: Callable[[], int] = now_millis,
    ) -> None:
        self._config = config
        self._cache = cache or RemoteCache()
        self._adapter = mesh_adapter or HostMeshAdapter()
        self._local_tmux = local_tmux or TmuxClient()
        self._refresher = refresher or RemoteRefresh(
            config, self._cache, mesh_adapter=self._adapter
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

    def load(self, *, start_refresh: bool = True) -> PickerModel:
        snapshot = self._adapter.load()
        local = self._local(snapshot, self._local_tmux, self._config)
        if snapshot is None:
            row = local.inventory(local.host.host_id, panes=False, option_names=())
            return PickerModel(
                {
                    "schemaVersion": 1,
                    "generatedAt": self._now(),
                    "meshRevision": None,
                    "hosts": [row],
                    "hostCatalog": self._catalog(None, local),
                    "remoteRefreshNeeded": False,
                    # An old Mesh marker must never surface in local-only
                    # mode, where it has no current revision to describe.
                    "remoteRefresh": None,
                },
                False,
            )
        local_row = local.inventory(snapshot.local_host.host_id, panes=False, option_names=())
        state = self._cache.load(snapshot)
        refresh_needed = self._remote_refresh_needed(snapshot, state)
        requested = self._refresher.request(snapshot) if start_refresh and refresh_needed else False
        payload, refresh_needed = self._snapshot_payload(
            snapshot, local, local_row, state, requested=requested
        )
        return PickerModel(
            payload,
            refresh_needed,
        )

    def refresh_now(self) -> PickerModel:
        """Run one revision-pinned foreground remote refresh, then reload.

        The Host Mesh adapter and RemoteRefresh owner retain their bounded
        process/deadline guarantees. This method does not spawn or retry a
        detached worker, so a failure remains observable to the callback and
        cannot turn into a refresh loop.
        """

        snapshot = self._adapter.load()
        if snapshot is not None:
            self._refresher.run(snapshot.revision)
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
            return PickerModel(
                {
                    "schemaVersion": 1,
                    "generatedAt": self._now(),
                    "meshRevision": None,
                    "hosts": [row],
                    "hostCatalog": self._catalog(None, local),
                    "remoteRefreshNeeded": False,
                    "remoteRefresh": None,
                },
                False,
            )
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
            return PickerModel(payload, refresh_needed)
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
        return PickerModel(payload, refresh_needed)

    def refresh_host_current(self, host_id: str) -> PickerModel:
        """Refresh one host against the currently loaded Mesh revision.

        Used only after an already-completed mutation whose old revision has
        become stale.  It is never an authority for the mutation itself.
        """
        snapshot = self._adapter.load()
        return self.refresh_host(host_id, None if snapshot is None else snapshot.revision)
