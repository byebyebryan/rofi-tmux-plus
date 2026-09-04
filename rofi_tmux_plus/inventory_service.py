"""One-snapshot local plus remote live inventory orchestration."""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait

from .config import Config
from .errors import ContractError, clean_message
from .host import LocalHost, local_host
from .lifecycle import LocalLifecycle, now_millis
from .mesh_adapter import HostMeshAdapter, MeshHost, MeshSnapshot, MeshStaleError
from .remote_inventory import RemoteInventory
from .tmux import TmuxClient, validate_user_option

_REMOTE_WORKERS = 4
_WHOLE_DEADLINE_SECONDS = 15.0
_REMOTE_HOST_DEADLINE_SECONDS = 8.0


class InventoryService:
    def __init__(
        self,
        config: Config,
        *,
        mesh_adapter: HostMeshAdapter | None = None,
        local_tmux: TmuxClient | None = None,
        remote_inventory: RemoteInventory | None = None,
        whole_deadline_seconds: float = _WHOLE_DEADLINE_SECONDS,
    ) -> None:
        self._config = config
        self._mesh_adapter = mesh_adapter or HostMeshAdapter()
        self._local_tmux = local_tmux or TmuxClient()
        self._remote_inventory = remote_inventory or RemoteInventory(self._mesh_adapter)
        self._whole_deadline_seconds = whole_deadline_seconds

    def inventory(
        self,
        *,
        requested_hosts: Sequence[str],
        mesh_revision: str | None,
        panes: bool,
        option_names: Sequence[str],
    ) -> dict[str, object]:
        option_names = tuple(dict.fromkeys(validate_user_option(name) for name in option_names))
        operation_deadline = time.monotonic() + self._whole_deadline_seconds
        snapshot = self._mesh_adapter.load()
        if snapshot is None:
            if mesh_revision is not None:
                raise ContractError(
                    "stale_mesh", "the current local-only host mesh has no revision"
                )
            fallback = local_host()
            selected = self._select_fallback(fallback, requested_hosts)
            local = LocalLifecycle(self._local_tmux, self._config, host=fallback)
            rows = [
                local.inventory(host.host_id, panes=panes, option_names=option_names)
                for host in selected
            ]
            return {
                "schemaVersion": 1,
                "generatedAt": now_millis(),
                "meshRevision": None,
                "hosts": rows,
            }
        if mesh_revision is not None and mesh_revision != snapshot.revision:
            raise ContractError("stale_mesh", "the Host Mesh changed; refresh and try again")
        selected = self._select_snapshot(snapshot, requested_hosts)
        return self._with_snapshot(
            snapshot,
            selected,
            panes=panes,
            option_names=option_names,
            deadline=operation_deadline,
        )

    @staticmethod
    def _select_fallback(host: LocalHost, requested: Sequence[str]) -> list[LocalHost]:
        if not requested:
            return [host]
        unique: set[str] = set()
        for value in requested:
            if value.casefold() != host.host_id.casefold() and not any(
                value.casefold() == alias.casefold() for alias in host.aliases
            ):
                raise ContractError("unknown_host", f"unknown local host: {value}", value)
            unique.add(host.host_id)
        return [host] if unique else []

    @staticmethod
    def _select_snapshot(snapshot: MeshSnapshot, requested: Sequence[str]) -> list[MeshHost]:
        if not requested:
            return list(snapshot.hosts)
        wanted = {snapshot.resolve_host(value).host_id for value in requested}
        # The request is a set but the live output always retains Mesh order.
        return [host for host in snapshot.hosts if host.host_id in wanted]

    def _with_snapshot(
        self,
        snapshot: MeshSnapshot,
        selected: Sequence[MeshHost],
        *,
        panes: bool,
        option_names: Sequence[str],
        deadline: float,
    ) -> dict[str, object]:
        mesh_local = snapshot.local_host
        fallback = local_host()
        local = LocalLifecycle(
            self._local_tmux,
            self._config,
            host=LocalHost(
                mesh_local.host_id,
                mesh_local.display,
                fallback.native_hostname,
                frozenset({mesh_local.host_id, *mesh_local.aliases}),
            ),
        )
        rows: dict[str, dict[str, object]] = {}
        remotes: list[MeshHost] = []
        for host in selected:
            if host.local:
                rows[host.host_id] = local.inventory(
                    host.host_id, panes=panes, option_names=option_names
                )
            else:
                remotes.append(host)
        if remotes:
            if deadline <= time.monotonic():
                for host in remotes:
                    rows[host.host_id] = self._remote_error(
                        host, "operation_failed", "remote inventory deadline exceeded"
                    )
                return self._response(snapshot, selected, rows)
            executor = ThreadPoolExecutor(max_workers=min(_REMOTE_WORKERS, len(remotes)))
            futures: dict[Future[dict[str, object]], MeshHost] = {}
            pending: set[Future[dict[str, object]]] = set()
            deadline_expired: set[Future[dict[str, object]]] = set()
            stale_error: MeshStaleError | None = None

            def capture(future: Future[dict[str, object]]) -> None:
                nonlocal stale_error
                host = futures[future]
                try:
                    rows[host.host_id] = future.result()
                except MeshStaleError as error:
                    stale_error = error
                except ContractError as error:
                    rows[host.host_id] = self._remote_error(host, error.code, error.message)
                except CancelledError:
                    rows[host.host_id] = self._remote_error(
                        host, "operation_failed", "remote inventory deadline exceeded"
                    )
                except Exception as error:  # noqa: BLE001 - per-host fault isolation
                    rows[host.host_id] = self._remote_error(
                        host, "operation_failed", clean_message(error)
                    )

            try:
                for host in remotes:
                    host_deadline = min(deadline, time.monotonic() + _REMOTE_HOST_DEADLINE_SECONDS)
                    futures[
                        executor.submit(
                            self._remote_inventory.inventory,
                            host,
                            snapshot.policy,
                            snapshot.revision,
                            panes=panes,
                            option_names=option_names,
                            deadline=host_deadline,
                        )
                    ] = host
                pending = set(futures)
                while pending and stale_error is None:
                    done, pending = wait(
                        pending,
                        timeout=max(0.0, deadline - time.monotonic()),
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        deadline_expired.update(pending)
                        break
                    for future in done:
                        capture(future)
            finally:
                for future in pending:
                    future.cancel()
                # Remote workers receive the shared deadline and must finish
                # before this command can expose any result or stale revision.
                executor.shutdown(wait=True, cancel_futures=True)
            for future, host in futures.items():
                if future in deadline_expired:
                    rows[host.host_id] = self._remote_error(
                        host, "operation_failed", "remote inventory deadline exceeded"
                    )
                if future.done():
                    try:
                        future.result()
                    except MeshStaleError as error:
                        stale_error = error
                    except (CancelledError, ContractError):
                        continue
                    except Exception:  # noqa: BLE001, S112 - only stale changes the response
                        continue
            if stale_error is not None:
                raise stale_error
        return self._response(snapshot, selected, rows)

    @staticmethod
    def _response(
        snapshot: MeshSnapshot,
        selected: Sequence[MeshHost],
        rows: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "generatedAt": now_millis(),
            "meshRevision": snapshot.revision,
            "hosts": [rows[host.host_id] for host in selected],
        }

    @staticmethod
    def _remote_error(host: MeshHost, code: str, message: str) -> dict[str, object]:
        return {
            "hostId": host.host_id,
            "display": host.display,
            "local": False,
            "status": "error",
            "observedAt": now_millis(),
            "nativeHostname": None,
            "serverGeneration": None,
            "route": None,
            "sessions": [],
            "error": {"code": code, "message": message},
        }
