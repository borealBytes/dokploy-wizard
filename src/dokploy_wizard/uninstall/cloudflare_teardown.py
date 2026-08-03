"""Receipt-authorized Cloudflare uninstall mutations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.networking import (
    ACCESS_APPLICATION_RESOURCE_TYPE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
    ACCESS_POLICY_RESOURCE_TYPE,
    DNS_RESOURCE_TYPE,
    TUNNEL_RESOURCE_TYPE,
)
from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.cloudflare_access_teardown import CloudflareAccessUninstallTeardown
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    CloudflareDnsDeletionClient,
    CloudflarePolicyDeletionClient,
    CloudflareTunnelDeletionClient,
    UninstallProviderClients,
    dns_fingerprint,
    policy_fingerprint,
    tunnel_fingerprint,
)


@dataclass(frozen=True, slots=True)
class CloudflareUninstallTeardown:
    """Deletes Cloudflare resources only after exact authority validation."""

    raw_input: RawEnvInput
    state_dir: Path | None
    providers: UninstallProviderClients

    def delete(self, deletion: PlannedDeletion) -> None:
        match deletion.resource.resource_type:
            case resource_type if resource_type == ACCESS_OTP_PROVIDER_RESOURCE_TYPE:
                CloudflareAccessUninstallTeardown(
                    self.raw_input, self.state_dir, self.providers
                ).delete_identity_provider(deletion)
            case resource_type if resource_type == ACCESS_APPLICATION_RESOURCE_TYPE:
                CloudflareAccessUninstallTeardown(
                    self.raw_input, self.state_dir, self.providers
                ).delete_application(deletion)
            case resource_type if resource_type == TUNNEL_RESOURCE_TYPE:
                self._delete_tunnel(deletion)
            case resource_type if resource_type == DNS_RESOURCE_TYPE:
                self._delete_dns(deletion)
            case resource_type if resource_type == ACCESS_POLICY_RESOURCE_TYPE:
                self._delete_policy(deletion)
            case resource_type:
                raise UninstallExecutionError(
                    f"Cloudflare teardown cannot delete resource type '{resource_type}'."
                )

    def _delete_tunnel(self, deletion: PlannedDeletion) -> None:
        authority_store = self._authority_store()
        authority = authority_store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError("Cloudflare tunnel lacks recorded uninstall authority.")
        if (
            authority.provider != "cloudflare"
            or authority.physical_target_id != deletion.resource.resource_id
        ):
            raise UninstallExecutionError(
                "Cloudflare tunnel authority does not bind the ledger target."
            )
        account_id = _account_id_from_scope(deletion.resource.scope)
        if authority.parent_target_id != account_id:
            raise UninstallExecutionError("Cloudflare tunnel authority does not bind the account.")
        cloudflare = self._tunnel_client()
        existing = cloudflare.get_tunnel(account_id, authority.physical_target_id)
        prior_deletion = authority_store.load_deletion(deletion.resource)
        if prior_deletion is not None:
            if existing is not None:
                raise UninstallExecutionError(
                    "Deleted Cloudflare tunnel reappeared after teardown."
                )
            return
        if existing is None:
            raise UninstallExecutionError("Recorded Cloudflare tunnel is missing before deletion.")
        if tunnel_fingerprint(existing) != authority.expected_fingerprint:
            raise UninstallExecutionError(
                "Cloudflare tunnel fingerprint changed since creation."
            )
        cloudflare.delete_tunnel(account_id, authority.physical_target_id)
        if cloudflare.get_tunnel(account_id, authority.physical_target_id) is not None:
            raise UninstallExecutionError("Cloudflare tunnel remains present after deletion.")
        authority_store.record_deletion(deletion.resource)

    def _delete_policy(self, deletion: PlannedDeletion) -> None:
        authority_store = self._authority_store()
        authority = authority_store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError(
                "Cloudflare Access policy lacks recorded uninstall authority."
            )
        if (
            authority.provider != "cloudflare_access_policy"
            or authority.physical_target_id != deletion.resource.resource_id
        ):
            raise UninstallExecutionError(
                "Cloudflare Access policy authority does not bind the ledger target."
            )
        account_id = _account_id_from_scope(deletion.resource.scope)
        cloudflare = self._policy_client()
        existing = cloudflare.get_access_policy(
            account_id,
            authority.parent_target_id,
            authority.physical_target_id,
        )
        prior_deletion = authority_store.load_deletion(deletion.resource)
        if prior_deletion is not None:
            if existing is not None:
                raise UninstallExecutionError(
                    "Deleted Cloudflare Access policy reappeared after teardown."
                )
            return
        if existing is None:
            raise UninstallExecutionError(
                "Recorded Cloudflare Access policy is missing before deletion."
            )
        if (
            existing.app_id != authority.parent_target_id
            or policy_fingerprint(existing) != authority.expected_fingerprint
        ):
            raise UninstallExecutionError(
                "Cloudflare Access policy fingerprint changed since creation."
            )
        cloudflare.delete_access_policy(
            account_id,
            authority.parent_target_id,
            authority.physical_target_id,
        )
        if (
            cloudflare.get_access_policy(
                account_id,
                authority.parent_target_id,
                authority.physical_target_id,
            )
            is not None
        ):
            raise UninstallExecutionError(
                "Cloudflare Access policy remains present after deletion."
            )
        authority_store.record_deletion(deletion.resource)

    def _delete_dns(self, deletion: PlannedDeletion) -> None:
        authority_store = self._authority_store()
        authority = authority_store.load_created(deletion.resource)
        if authority is None:
            raise UninstallExecutionError(
                "Cloudflare DNS record lacks recorded uninstall authority."
            )
        if (
            authority.provider != "cloudflare_dns"
            or authority.physical_target_id != deletion.resource.resource_id
        ):
            raise UninstallExecutionError(
                "Cloudflare DNS authority does not bind the ledger target."
            )
        zone_id = _zone_id_from_scope(deletion.resource.scope)
        if authority.parent_target_id != zone_id:
            raise UninstallExecutionError("Cloudflare DNS authority does not bind the zone.")
        cloudflare = self._dns_client()
        existing = cloudflare.get_dns_record(zone_id, authority.physical_target_id)
        prior_deletion = authority_store.load_deletion(deletion.resource)
        if prior_deletion is not None:
            if existing is not None:
                raise UninstallExecutionError(
                    "Deleted Cloudflare DNS record reappeared after teardown."
                )
            return
        if existing is None:
            raise UninstallExecutionError(
                "Recorded Cloudflare DNS record is missing before deletion."
            )
        if dns_fingerprint(existing) != authority.expected_fingerprint:
            raise UninstallExecutionError(
                "Cloudflare DNS record fingerprint changed since creation."
            )
        cloudflare.delete_dns_record(zone_id, authority.physical_target_id)
        if cloudflare.get_dns_record(zone_id, authority.physical_target_id) is not None:
            raise UninstallExecutionError("Cloudflare DNS record remains present after deletion.")
        authority_store.record_deletion(deletion.resource)

    def _authority_store(self) -> UninstallAuthorityStore:
        if self.state_dir is None:
            raise UninstallExecutionError("Cloudflare teardown requires state.")
        return UninstallAuthorityStore(self.state_dir)

    def _tunnel_client(self) -> CloudflareTunnelDeletionClient:
        if self.providers.cloudflare is not None:
            return self.providers.cloudflare
        return CloudflareApiBackend(self.raw_input)

    def _dns_client(self) -> CloudflareDnsDeletionClient:
        if self.providers.cloudflare_dns is not None:
            return self.providers.cloudflare_dns
        return CloudflareApiBackend(self.raw_input)

    def _policy_client(self) -> CloudflarePolicyDeletionClient:
        if self.providers.cloudflare_access_policy is not None:
            return self.providers.cloudflare_access_policy
        return CloudflareApiBackend(self.raw_input)


def _account_id_from_scope(scope: str) -> str:
    parts = scope.split(":", maxsplit=2)
    if len(parts) < 2 or parts[0] != "account" or parts[1] == "":
        raise UninstallExecutionError("Cloudflare account ledger scope is invalid.")
    return parts[1]


def _zone_id_from_scope(scope: str) -> str:
    parts = scope.split(":", maxsplit=2)
    if len(parts) != 3 or parts[0] != "zone" or parts[1] == "" or parts[2] == "":
        raise UninstallExecutionError("Cloudflare DNS ledger scope is invalid.")
    return parts[1]
