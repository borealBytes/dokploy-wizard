from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Final, assert_never

from dokploy_wizard.core import SHARED_NETWORK_RESOURCE_TYPE
from dokploy_wizard.networking import (
    ACCESS_APPLICATION_RESOURCE_TYPE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
    ACCESS_POLICY_RESOURCE_TYPE,
    DNS_RESOURCE_TYPE,
    TUNNEL_RESOURCE_TYPE,
)
from dokploy_wizard.state import OwnedResource
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.families import ProviderFamily, family_for
from dokploy_wizard.uninstall.planner import PlannedDeletion

_OWNER_ID: Final = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_CLOUDFLARE_PROVIDERS: Final = {
    TUNNEL_RESOURCE_TYPE: "cloudflare",
    DNS_RESOURCE_TYPE: "cloudflare_dns",
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE: "cloudflare_access_identity_provider",
    ACCESS_APPLICATION_RESOURCE_TYPE: "cloudflare_access_application",
    ACCESS_POLICY_RESOURCE_TYPE: "cloudflare_access_policy",
}


def seed_creation_authority(state_dir: Path, resources: tuple[OwnedResource, ...]) -> None:
    store = UninstallAuthorityStore(state_dir)
    for resource in resources:
        provider, physical_target, parent_target = _provider_target(resource)
        identity = "\0".join((resource.resource_type, resource.resource_id, resource.scope))
        store.record_created(
            resource=resource,
            owner_id=_OWNER_ID,
            provider=provider,
            physical_target_id=physical_target,
            parent_target_id=parent_target,
            expected_fingerprint=sha256(identity.encode()).hexdigest(),
        )


class RecordingAuthorityBackend:
    """Test backend that checkpoints each simulated provider deletion."""

    def __init__(self, state_dir: Path) -> None:
        self._store = UninstallAuthorityStore(state_dir)
        self.deleted: list[PlannedDeletion] = []

    def delete(self, deletion: PlannedDeletion) -> None:
        self.deleted.append(deletion)
        self._store.record_deletion(deletion.resource)


def _provider_target(resource: OwnedResource) -> tuple[str, str, str]:
    match family_for(resource.resource_type):
        case ProviderFamily.SCHEDULE:
            return "dokploy_schedule", resource.resource_id, "dokploy:nextcloud-stack"
        case ProviderFamily.CLOUDFLARE:
            return (
                _CLOUDFLARE_PROVIDERS[resource.resource_type],
                resource.resource_id,
                resource.scope,
            )
        case ProviderFamily.TAILSCALE:
            return "tailscale", resource.resource_id, "tailnet:test"
        case ProviderFamily.DOKPLOY:
            return (
                "dokploy_compose",
                f"dokploy-compose:{resource.resource_id}",
                "dokploy-project:nextcloud-stack",
            )
        case ProviderFamily.DOCKER:
            provider = (
                "docker_network"
                if resource.resource_type == SHARED_NETWORK_RESOURCE_TYPE
                else "docker_volume"
            )
            return provider, resource.resource_id, "docker:nextcloud-stack"
        case unexpected:
            assert_never(unexpected)
