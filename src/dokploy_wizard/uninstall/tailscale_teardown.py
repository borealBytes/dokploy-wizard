"""Receipt-authorized Tailscale uninstall mutations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.tailscale import ShellTailscaleBackend
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    TailscaleDeletionClient,
    UninstallProviderClients,
    tailscale_fingerprint,
)


@dataclass(frozen=True, slots=True)
class TailscaleUninstallTeardown:
    """Disconnects the exact receipt-authorized Tailscale node."""

    raw_input: RawEnvInput
    state_dir: Path | None
    providers: UninstallProviderClients

    def delete(self, deletion: PlannedDeletion) -> None:
        if self.state_dir is None:
            raise UninstallExecutionError("Tailscale teardown requires state.")
        authority_store = UninstallAuthorityStore(self.state_dir)
        authority = authority_store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError("Tailscale node lacks recorded uninstall authority.")
        if (
            authority.provider != "tailscale"
            or authority.physical_target_id != deletion.resource.resource_id
            or authority.parent_target_id != deletion.resource.scope
        ):
            raise UninstallExecutionError("Tailscale authority does not bind the ledger target.")
        client = self._client()
        existing = client.get_node(authority.physical_target_id)
        prior_deletion = authority_store.load_deletion(deletion.resource)
        if prior_deletion is not None:
            if existing is not None:
                raise UninstallExecutionError("Deleted Tailscale node reappeared after teardown.")
            return
        if existing is None:
            raise UninstallExecutionError("Recorded Tailscale node is missing before deletion.")
        if (
            existing.resource_id != authority.physical_target_id
            or tailscale_fingerprint(existing) != authority.expected_fingerprint
        ):
            raise UninstallExecutionError("Tailscale node fingerprint changed since creation.")
        client.disconnect()
        if client.get_node(authority.physical_target_id) is not None:
            raise UninstallExecutionError("Tailscale node remains present after deletion.")
        authority_store.record_deletion(deletion.resource)

    def _client(self) -> TailscaleDeletionClient:
        if self.providers.tailscale is not None:
            return self.providers.tailscale
        return ShellTailscaleBackend(self.raw_input)
