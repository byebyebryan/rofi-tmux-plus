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
from .viewer_service import close_viewer, effective_destroy_unattached, inspect_viewers


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
        required_options: Sequence[tuple[str, str]] = (),
        verified_viewer: bool = False,
    ) -> dict[str, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            return self._mesh_response(
                local.open(
                    host_id,
                    None,
                    generation,
                    session_id,
                    created_at,
                    expected_name,
                    required_options,
                    verified_viewer,
                ),
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
            required_options,
            verified_viewer,
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

    def _viewer_context(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None,
        required_options: Sequence[tuple[str, str]],
    ) -> tuple[object, object, str | None, object]:
        snapshot, host, local = self._selected(host_id, revision)
        if snapshot is None or host is None or host.local:
            session = local.validate_reference(
                host_id,
                None,
                generation,
                session_id,
                created_at,
                expected_name,
                required_options,
            )
            try:
                destroy_value = effective_destroy_unattached(local.tmux, session_id)
            except ContractError:
                destroy_value = None
            inspection = inspect_viewers(
                session,
                self._config,
                local_tmux=local.tmux,
                destroy_unattached=destroy_value,
            )
            return session, inspection, snapshot.revision if snapshot else None, (local, None, None)

        result = self._remote.viewers(
            host,
            snapshot.policy,
            snapshot.revision,
            generation,
            session_id,
            created_at,
            expected_name,
            required_options,
        )
        assert result.session is not None
        inspection = inspect_viewers(
            result.session,
            self._config,
            remote_route=result.route,
            remote_executable=snapshot.policy.executable,
            remote_native_hostname=result.native_hostname,
            destroy_unattached=result.destroy_unattached,
        )
        return result.session, inspection, snapshot.revision, (host, snapshot.policy, result)

    def viewers(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        expected_name: str | None = None,
        required_options: Sequence[tuple[str, str]] = (),
    ) -> dict[str, object]:
        session, inspection, mesh_revision, _context = self._viewer_context(
            host_id,
            revision,
            generation,
            session_id,
            created_at,
            expected_name,
            required_options,
        )
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": mesh_revision,
            "sessionRef": session.reference.as_dict(),
            **inspection.as_fields(),
        }

    def close_viewer(
        self,
        host_id: str,
        revision: str | None,
        generation: str,
        session_id: str,
        created_at: int,
        viewer_id: str,
        expected_name: str | None = None,
        required_options: Sequence[tuple[str, str]] = (),
    ) -> dict[str, object]:
        args = (
            host_id,
            revision,
            generation,
            session_id,
            created_at,
            expected_name,
            required_options,
        )
        session, inspection, mesh_revision, _context = self._viewer_context(*args)
        if not inspection.close_safe:
            raise ContractError(
                "viewer_destroy_guard", "destroy-unattached is not provably off", host_id
            )
        if inspection.status == "unsupported":
            raise ContractError(
                "viewer_unsupported", inspection.reason or "viewer close is unsupported", host_id
            )
        if inspection.status == "ambiguous":
            raise ContractError(
                "viewer_ambiguous", inspection.reason or "viewer identity is ambiguous", host_id
            )
        if inspection.status == "unverified":
            raise ContractError(
                "viewer_unverified", inspection.reason or "viewer identity is unverified", host_id
            )
        viewer = next((row for row in inspection.viewers if row.viewer_id == viewer_id), None)
        if viewer is None:
            return self._close_response(
                session, mesh_revision, viewer_id, closed=False, already_closed=True
            )

        def revalidate():
            current_session, current_inspection, _current_revision, _ = self._viewer_context(*args)
            if current_session.reference != session.reference:
                raise ContractError(
                    "stale_session", "the selected tmux session changed before close", host_id
                )
            return current_inspection

        def validate_session() -> bool:
            try:
                current_session, current_inspection, _current_revision, _ = self._viewer_context(
                    *args
                )
            except ContractError:
                return False
            return current_session.reference == session.reference and current_inspection.close_safe

        closed = close_viewer(viewer, revalidate=revalidate, validate_session=validate_session)
        return self._close_response(
            session, mesh_revision, viewer_id, closed=closed, already_closed=not closed
        )

    @staticmethod
    def _close_response(
        session: object,
        mesh_revision: str | None,
        viewer_id: str,
        *,
        closed: bool,
        already_closed: bool,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "ok": True,
            "meshRevision": mesh_revision,
            "sessionRef": session.reference.as_dict(),
            "viewerId": viewer_id,
            "closed": closed,
            "alreadyClosed": already_closed,
        }
