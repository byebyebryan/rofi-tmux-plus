"""One-snapshot local plus remote live inventory orchestration."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait

from .config import Config
from .errors import ContractError, clean_message
from .host import LocalHost, local_host
from .lifecycle import LocalLifecycle, now_millis
from .mesh_adapter import HostMeshAdapter, MeshHost, MeshSnapshot, MeshStaleError
from .model import Session, SessionReference
from .remote_inventory import RemoteInventory
from .tmux import TmuxClient, validate_user_option
from .viewer_service import LocalViewerObservation, ViewerTarget, observe_local_viewers

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
        with_viewers: bool = False,
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
            response: dict[str, object] = {
                "schemaVersion": 1,
                "generatedAt": now_millis(),
                "meshRevision": None,
                "hosts": rows,
            }
            if with_viewers:
                self._enrich_local_viewers(
                    response, fallback.host_id, None, deadline=operation_deadline
                )
            return response
        if mesh_revision is not None and mesh_revision != snapshot.revision:
            raise ContractError("stale_mesh", "the Host Mesh changed; refresh and try again")
        selected = self._select_snapshot(snapshot, requested_hosts)
        return self._with_snapshot(
            snapshot,
            selected,
            panes=panes,
            option_names=option_names,
            deadline=operation_deadline,
            with_viewers=with_viewers,
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
        with_viewers: bool,
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
            else:
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
                        host_deadline = min(
                            deadline, time.monotonic() + _REMOTE_HOST_DEADLINE_SECONDS
                        )
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
        response = self._response(snapshot, selected, rows)
        if with_viewers:
            self._enrich_local_viewers(
                response,
                snapshot.local_host.host_id,
                snapshot.policy.executable,
                deadline=deadline,
            )
        return response

    def _enrich_local_viewers(
        self,
        response: dict[str, object],
        endpoint_host_id: str,
        remote_executable: str | None,
        *,
        deadline: float,
    ) -> None:
        rows = response.get("hosts")
        targets: list[ViewerTarget] = []
        if isinstance(rows, list):
            for host in rows:
                if not isinstance(host, dict) or not isinstance(host.get("sessions"), list):
                    continue
                for raw in host["sessions"]:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        reference = SessionReference(
                            str(raw["hostId"]),
                            str(raw["serverGeneration"]),
                            str(raw["sessionId"]),
                            int(raw["createdAt"]),
                        )
                    except (KeyError, TypeError, ValueError, OverflowError):
                        raw["localViewer"] = {"state": "unknown", "reason": "inventory_incomplete"}
                        continue
                    session = Session(
                        reference,
                        raw.get("name") if isinstance(raw.get("name"), str) else None,
                        None,
                        None,
                        raw.get("attachedClients")
                        if type(raw.get("attachedClients")) is int
                        else None,
                        bool(raw.get("pending")),
                        None,
                        None,
                        None,
                        None,
                    )
                    native_hostname = host.get("nativeHostname")
                    targets.append(
                        ViewerTarget(
                            session,
                            host.get("local") is True,
                            native_hostname if isinstance(native_hostname, str) else None,
                            host.get("route") if isinstance(host.get("route"), str) else None,
                            remote_executable or "ssh",
                        )
                    )
        try:
            batch = observe_local_viewers(
                targets,
                self._config,
                local_tmux=self._local_tmux,
                deadline=deadline,
            )
        except Exception:  # noqa: BLE001 - viewer failure must not replace owner inventory
            batch = None
        if batch is None:
            observations: Mapping[SessionReference, LocalViewerObservation] = {
                target.session.reference: LocalViewerObservation(
                    "unknown", reason="inventory_incomplete"
                )
                for target in targets
            }
            observed_at = now_millis()
        else:
            observations = batch.observations
            observed_at = batch.observed_at
        for host in rows if isinstance(rows, list) else ():
            if not isinstance(host, dict) or not isinstance(host.get("sessions"), list):
                continue
            for session in host["sessions"]:
                if not isinstance(session, dict):
                    continue
                try:
                    reference = SessionReference(
                        str(session["hostId"]),
                        str(session["serverGeneration"]),
                        str(session["sessionId"]),
                        int(session["createdAt"]),
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    session["localViewer"] = {"state": "unknown", "reason": "inventory_incomplete"}
                    continue
                observation = observations.get(reference)
                session["localViewer"] = (
                    observation.as_dict()
                    if observation is not None
                    else {"state": "unknown", "reason": "inventory_incomplete"}
                )
        response["viewerEndpoint"] = {"hostId": endpoint_host_id, "observedAt": observed_at}

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
