"""Receipt-authorized Cloudflare Access teardown."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_authority_schema import UninstallAuthority
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    CloudflareApplicationDeletionClient,
    CloudflareIdentityProviderDeletionClient,
    UninstallProviderClients,
    application_fingerprint,
    identity_provider_fingerprint,
)

CloudflareTarget = TypeVar("CloudflareTarget")


@dataclass(frozen=True, slots=True)
class CloudflareAccessUninstallTeardown:
    raw_input: RawEnvInput
    state_dir: Path | None
    providers: UninstallProviderClients

    def delete_identity_provider(self, deletion: PlannedDeletion) -> None:
        authority = self._authority(deletion)
        if authority.provider != "cloudflare_access_identity_provider":
            raise UninstallExecutionError("Cloudflare Access OTP authority is invalid.")
        account_id = _account_id(deletion.resource.scope)
        if authority.parent_target_id != account_id:
            raise UninstallExecutionError("Cloudflare Access OTP authority binds another account.")
        client = self._identity_provider_client()
        existing = client.get_access_identity_provider(account_id, authority.physical_target_id)
        self._delete(
            deletion,
            existing,
            authority.expected_fingerprint,
            identity_provider_fingerprint,
            lambda: client.delete_access_identity_provider(
                account_id, authority.physical_target_id
            ),
            lambda: client.get_access_identity_provider(
                account_id, authority.physical_target_id
            ),
        )

    def delete_application(self, deletion: PlannedDeletion) -> None:
        authority = self._authority(deletion)
        if authority.provider != "cloudflare_access_application":
            raise UninstallExecutionError("Cloudflare Access application authority is invalid.")
        account_id = _account_id(deletion.resource.scope)
        if authority.parent_target_id != account_id:
            raise UninstallExecutionError(
                "Cloudflare Access application authority binds another account."
            )
        client = self._application_client()
        existing = client.get_access_application(account_id, authority.physical_target_id)
        self._delete(
            deletion,
            existing,
            authority.expected_fingerprint,
            application_fingerprint,
            lambda: client.delete_access_application(account_id, authority.physical_target_id),
            lambda: client.get_access_application(account_id, authority.physical_target_id),
        )

    def _authority(self, deletion: PlannedDeletion) -> UninstallAuthority:
        if self.state_dir is None:
            raise UninstallExecutionError("Cloudflare Access teardown requires state.")
        authority = UninstallAuthorityStore(self.state_dir).load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError("Cloudflare Access resource lacks recorded authority.")
        return authority

    def _delete(
        self,
        deletion: PlannedDeletion,
        existing: CloudflareTarget | None,
        expected: str,
        fingerprint: Callable[[CloudflareTarget], str],
        delete: Callable[[], None],
        reread: Callable[[], CloudflareTarget | None],
    ) -> None:
        if self.state_dir is None:
            raise UninstallExecutionError("Cloudflare Access teardown requires state.")
        store = UninstallAuthorityStore(self.state_dir)
        if store.load_deletion(deletion.resource) is not None:
            if reread() is not None:
                raise UninstallExecutionError("Deleted Cloudflare Access resource reappeared.")
            return
        if existing is None or fingerprint(existing) != expected:
            raise UninstallExecutionError("Cloudflare Access resource changed since creation.")
        delete()
        if reread() is not None:
            raise UninstallExecutionError(
                "Cloudflare Access resource remains present after deletion."
            )
        store.record_deletion(deletion.resource)

    def _application_client(self) -> CloudflareApplicationDeletionClient:
        return self.providers.cloudflare_access_application or CloudflareApiBackend(self.raw_input)

    def _identity_provider_client(self) -> CloudflareIdentityProviderDeletionClient:
        return self.providers.cloudflare_access_identity_provider or CloudflareApiBackend(
            self.raw_input
        )


def _account_id(scope: str) -> str:
    parts = scope.split(":", maxsplit=2)
    if len(parts) < 2 or parts[0] != "account" or parts[1] == "":
        raise UninstallExecutionError("Cloudflare account ledger scope is invalid.")
    return parts[1]
