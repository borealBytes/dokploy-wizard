"""Receipt-authorized Dokploy compose teardown."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.client import DokployApiClient
from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    DokployComposeDeletionRecord,
    DokployDeletionClient,
    UninstallProviderClients,
    dokploy_compose_fingerprint,
)


class _DokployApiAdapter:
    def __init__(self, client: DokployApiClient) -> None:
        self._client = client

    def get_compose(
        self, project_id: str, compose_id: str
    ) -> DokployComposeDeletionRecord | None:
        compose = self._client.get_compose(project_id=project_id, compose_id=compose_id)
        if compose is None:
            return None
        return DokployComposeDeletionRecord(
            compose_id=compose.compose_id,
            project_id=project_id,
            name=compose.name,
        )

    def delete_compose(self, compose_id: str) -> None:
        self._client.delete_compose(compose_id=compose_id)


@dataclass(slots=True)
class DokployUninstallTeardown:
    """Deletes exact receipt-authorized compose targets once per physical target."""

    raw_input: RawEnvInput
    state_dir: Path | None
    providers: UninstallProviderClients
    deleted_targets: set[tuple[str, str]]

    def delete(self, deletion: PlannedDeletion) -> None:
        store = self._authority_store()
        authority = store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError("Dokploy compose lacks recorded uninstall authority.")
        if authority.provider != "dokploy_compose":
            raise UninstallExecutionError("Dokploy authority does not bind a compose target.")
        client = self._client()
        existing = client.get_compose(authority.parent_target_id, authority.physical_target_id)
        if store.load_deletion(deletion.resource) is not None:
            if existing is not None:
                raise UninstallExecutionError("Deleted Dokploy compose reappeared after teardown.")
            return
        target_key = (authority.parent_target_id, authority.physical_target_id)
        if target_key in self.deleted_targets:
            if existing is not None:
                raise UninstallExecutionError("Deleted Dokploy compose reappeared during teardown.")
            store.record_deletion(deletion.resource)
            return
        if existing is None:
            raise UninstallExecutionError("Recorded Dokploy compose is missing before deletion.")
        if dokploy_compose_fingerprint(existing) != authority.expected_fingerprint:
            raise UninstallExecutionError("Dokploy compose fingerprint changed since creation.")
        client.delete_compose(authority.physical_target_id)
        if client.get_compose(authority.parent_target_id, authority.physical_target_id) is not None:
            raise UninstallExecutionError("Dokploy compose remains present after deletion.")
        self.deleted_targets.add(target_key)
        store.record_deletion(deletion.resource)

    def _authority_store(self) -> UninstallAuthorityStore:
        if self.state_dir is None:
            raise UninstallExecutionError("Dokploy teardown requires state.")
        return UninstallAuthorityStore(self.state_dir)

    def _client(self) -> DokployDeletionClient:
        if self.providers.dokploy is not None:
            return self.providers.dokploy
        api_url = self.raw_input.values.get("DOKPLOY_API_URL")
        api_key = self.raw_input.values.get("DOKPLOY_API_KEY")
        if api_url is None or api_key is None:
            raise UninstallExecutionError("Dokploy teardown requires runtime API credentials.")
        return _DokployApiAdapter(DokployApiClient(api_url=api_url, api_key=api_key))
