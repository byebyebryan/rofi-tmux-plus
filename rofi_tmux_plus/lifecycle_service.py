"""One-Host-Mesh-snapshot router for local and remote lifecycle commands."""

from __future__ import annotations

from collections.abc import Sequence

from .config import Config
from .errors import ContractError
from .host import LocalHost, local_host
from .lifecycle import LocalLifecycle, focus_session_window
from .mesh_adapter import HostMeshAdapter, MeshHost, MeshSnapshot
from .remote_lifecycle import RemoteLifecycle
from .tmux import TmuxClient


class LifecycleService:
    """Select one logical host from exactly one provider snapshot per action."""

    def __init__(
        self,
        config: Config,
        *,
        mesh_adapter: HostMeshAdapter | None = None,
        local_tmux: TmuxClient | None = None,
        remote_lifecycle: RemoteLifecycle | None = None,
    ) -> None:
        self._config = config
        self._adapter = mesh_adapter or HostMeshAdapter()
        self._local_tmux = local_tmux or TmuxClient()
        self._remote = remote_lifecycle or RemoteLifecycle(
            self._adapter,
            config,
            focus=lambda session, native: focus_session_window(session.name, native),
        )

    def _selected(
        self, host_id: str, revision: str | None
    ) -> tuple[MeshSnapshot | None, MeshHost | None, LocalLifecycle]:
        snapshot = self._adapter.load()
        if snapshot is None:
            if revision is not None:
                raise ContractError(
                    "stale_mesh", "the current local-only host mesh has no revision", host_id
                )
            return None, None, LocalLifecycle(self._local_tmux, self._config, host=local_host())
        if revision is not None and revision != snapshot.revision:
            raise ContractError(
                "stale_mesh", "the Host Mesh changed; refresh and try again", host_id
            )
        host = snapshot.resolve_host(host_id)
        native = local_host().native_hostname
        mesh_local = snapshot.local_host
        local = LocalLifecycle(
            self._local_tmux,
            self._config,
            host=LocalHost(
                mesh_local.host_id,
                mesh_local.display,
                native,
                frozenset({mesh_local.host_id, *mesh_local.aliases}),
            ),
        )
        return snapshot, host, local

    @staticmethod
    def _mesh_response(response: dict[str, object], revision: str | None) -> dict[str, object]:
        response["meshRevision"] = revision
        return response

    def open(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None = None,
    ) -> dict[str, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            return self._mesh_response(
                local.open(host_id, None, generation, session_id, created_at, expected_name),
                snapshot.revision if snapshot else None,
            )
        return self._remote.open(
            host,
            snapshot.policy,
            snapshot.revision,
            generation,
            session_id,
            created_at,
            expected_name,
        )

    def create(
        self,
        host_id: str,
        revision: str | None,
        name: str,
        cwd: str | None,
        options: Sequence[tuple[str, str]],
        command: Sequence[str],
        defer_until_attached: bool,
        attach_timeout: int | None,
        open_after: bool,
    ) -> dict[str, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            return self._mesh_response(
                local.create(
                    host_id,
                    None,
                    name,
                    cwd,
                    options,
                    command,
                    defer_until_attached,
                    attach_timeout,
                    open_after,
                ),
                snapshot.revision if snapshot else None,
            )
        return self._remote.create(
            host,
            snapshot.policy,
            snapshot.revision,
            name,
            cwd,
            options,
            command,
            defer_until_attached,
            attach_timeout,
            open_after,
        )

    def rename(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
        name: str,
    ) -> dict[str, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            return self._mesh_response(
                local.rename(
                    host_id, None, generation, session_id, created_at, expected_name, name
                ),
                snapshot.revision if snapshot else None,
            )
        return self._remote.rename(
            host,
            snapshot.policy,
            snapshot.revision,
            generation,
            session_id,
            created_at,
            expected_name,
            name,
        )

    def kill(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str,
    ) -> dict[str, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            return self._mesh_response(
                local.kill(host_id, None, generation, session_id, created_at, expected_name),
                snapshot.revision if snapshot else None,
            )
        return self._remote.kill(
            host,
            snapshot.policy,
            snapshot.revision,
            generation,
            session_id,
            created_at,
            expected_name,
        )
