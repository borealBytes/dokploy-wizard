"""Receipt-authorized Docker volume and network teardown."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.docker_client import SubprocessDockerDeletionClient
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    DockerDeletionClient,
    DockerNetworkRecord,
    DockerVolumeRecord,
    UninstallProviderClients,
    docker_network_fingerprint,
    docker_volume_fingerprint,
)


@dataclass(slots=True)
class DockerUninstallTeardown:
    """Deletes exact receipt-authorized Docker volume/network targets."""

    state_dir: Path | None
    providers: UninstallProviderClients
    deleted_targets: set[tuple[str, str]]

    def delete(self, deletion: PlannedDeletion) -> None:
        store = self._authority_store()
        authority = store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError("Docker target lacks recorded uninstall authority.")
        match authority.provider:
            case "docker_volume":
                self._delete_volume(
                    deletion, authority.physical_target_id, authority.expected_fingerprint
                )
            case "docker_network":
                self._delete_network(
                    deletion, authority.physical_target_id, authority.expected_fingerprint
                )
            case _:
                raise UninstallExecutionError("Docker authority does not bind a supported target.")

    def _delete_volume(self, deletion: PlannedDeletion, target_id: str, fingerprint: str) -> None:
        client = self._client()
        existing = client.get_volume(target_id)
        self._delete_target(
            deletion=deletion,
            kind="volume",
            target_id=target_id,
            existing=existing,
            fingerprint=(None if existing is None else docker_volume_fingerprint(existing)),
            delete=lambda: client.delete_volume(target_id),
            reread=lambda: client.get_volume(target_id),
            expected=fingerprint,
        )

    def _delete_network(self, deletion: PlannedDeletion, target_id: str, fingerprint: str) -> None:
        client = self._client()
        existing = client.get_network(target_id)
        self._delete_target(
            deletion=deletion,
            kind="network",
            target_id=target_id,
            existing=existing,
            fingerprint=(None if existing is None else docker_network_fingerprint(existing)),
            delete=lambda: client.delete_network(target_id),
            reread=lambda: client.get_network(target_id),
            expected=fingerprint,
        )

    def _delete_target(
        self,
        *,
        deletion: PlannedDeletion,
        kind: str,
        target_id: str,
        existing: DockerVolumeRecord | DockerNetworkRecord | None,
        fingerprint: str | None,
        delete: Callable[[], None],
        reread: Callable[[], DockerVolumeRecord | DockerNetworkRecord | None],
        expected: str,
    ) -> None:
        store = self._authority_store()
        if store.load_deletion(deletion.resource) is not None:
            if reread() is not None:
                raise UninstallExecutionError(f"Deleted Docker {kind} reappeared after teardown.")
            return
        target_key = (kind, target_id)
        if target_key in self.deleted_targets:
            if reread() is not None:
                raise UninstallExecutionError(f"Deleted Docker {kind} reappeared during teardown.")
            store.record_deletion(deletion.resource)
            return
        if existing is None:
            raise UninstallExecutionError(f"Recorded Docker {kind} is missing before deletion.")
        if fingerprint != expected:
            raise UninstallExecutionError(f"Docker {kind} fingerprint changed since creation.")
        delete()
        if reread() is not None:
            raise UninstallExecutionError(f"Docker {kind} remains present after deletion.")
        self.deleted_targets.add(target_key)
        store.record_deletion(deletion.resource)

    def _authority_store(self) -> UninstallAuthorityStore:
        if self.state_dir is None:
            raise UninstallExecutionError("Docker teardown requires state.")
        return UninstallAuthorityStore(self.state_dir)

    def _client(self) -> DockerDeletionClient:
        return self.providers.docker or SubprocessDockerDeletionClient()
