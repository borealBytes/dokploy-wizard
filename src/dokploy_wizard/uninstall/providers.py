"""Narrow production provider capabilities for receipt-authorized uninstall."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    application_fingerprint as _application_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    dns_fingerprint as _dns_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    policy_fingerprint as _policy_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    tunnel_fingerprint as _tunnel_fingerprint,
)
from dokploy_wizard.state.uninstall_targets import (
    DockerNetworkRecord as DockerNetworkRecord,
)
from dokploy_wizard.state.uninstall_targets import (
    DockerVolumeRecord as DockerVolumeRecord,
)
from dokploy_wizard.state.uninstall_targets import (
    DokployComposeDeletionRecord as DokployComposeDeletionRecord,
)
from dokploy_wizard.state.uninstall_targets import (
    docker_network_fingerprint as _docker_network_fingerprint,
)
from dokploy_wizard.state.uninstall_targets import (
    docker_volume_fingerprint as _docker_volume_fingerprint,
)
from dokploy_wizard.state.uninstall_targets import (
    dokploy_compose_fingerprint as _dokploy_compose_fingerprint,
)
from dokploy_wizard.tailscale import TailscaleManagedResource


class CloudflareTunnelDeletionClient(Protocol):
    """Minimum Cloudflare capability required for tunnel teardown."""

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None: ...

    def delete_tunnel(self, account_id: str, tunnel_id: str) -> None: ...


class CloudflareDnsDeletionClient(Protocol):
    """Minimum Cloudflare capability required for DNS record teardown."""

    def get_dns_record(self, zone_id: str, record_id: str) -> CloudflareDnsRecord | None: ...

    def delete_dns_record(self, zone_id: str, record_id: str) -> None: ...


class CloudflarePolicyDeletionClient(Protocol):
    """Minimum Cloudflare capability required for Access policy teardown."""

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None: ...

    def delete_access_policy(self, account_id: str, app_id: str, policy_id: str) -> None: ...


class CloudflareApplicationDeletionClient(Protocol):
    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None: ...

    def delete_access_application(self, account_id: str, app_id: str) -> None: ...


class CloudflareIdentityProviderDeletionClient(Protocol):
    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None: ...

    def delete_access_identity_provider(self, account_id: str, provider_id: str) -> None: ...


class TailscaleDeletionClient(Protocol):
    """Minimum Tailscale capability required for node teardown."""

    def get_node(self, resource_id: str) -> TailscaleManagedResource | None: ...

    def disconnect(self) -> None: ...


class DokployDeletionClient(Protocol):
    """Minimum Dokploy capability required for compose teardown."""

    def get_compose(
        self, project_id: str, compose_id: str
    ) -> DokployComposeDeletionRecord | None: ...

    def delete_compose(self, compose_id: str) -> None: ...


class DockerDeletionClient(Protocol):
    """Minimum Docker capability required for exact volume/network teardown."""

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None: ...

    def delete_volume(self, volume_id: str) -> None: ...

    def get_network(self, network_id: str) -> DockerNetworkRecord | None: ...

    def delete_network(self, network_id: str) -> None: ...


def tunnel_fingerprint(tunnel: CloudflareTunnel) -> str:
    return _tunnel_fingerprint(tunnel)


def dns_fingerprint(record: CloudflareDnsRecord) -> str:
    return _dns_fingerprint(record)


def policy_fingerprint(policy: CloudflareAccessPolicy) -> str:
    return _policy_fingerprint(policy)


def application_fingerprint(application: CloudflareAccessApplication) -> str:
    return _application_fingerprint(application)


def identity_provider_fingerprint(provider: CloudflareAccessIdentityProvider) -> str:
    value = f"{provider.provider_id}\0{provider.name}\0{provider.provider_type}"
    return sha256(value.encode()).hexdigest()


def tailscale_fingerprint(node: TailscaleManagedResource) -> str:
    return sha256(f"{node.resource_id}\0{node.resource_name}".encode()).hexdigest()


def dokploy_compose_fingerprint(compose: DokployComposeDeletionRecord) -> str:
    return _dokploy_compose_fingerprint(compose)


def docker_volume_fingerprint(volume: DockerVolumeRecord) -> str:
    return _docker_volume_fingerprint(volume)


def docker_network_fingerprint(network: DockerNetworkRecord) -> str:
    return _docker_network_fingerprint(network)


@dataclass(frozen=True, slots=True)
class UninstallProviderClients:
    """Optional provider overrides for uninstall tests and controlled callers."""

    cloudflare: CloudflareTunnelDeletionClient | None = None
    cloudflare_dns: CloudflareDnsDeletionClient | None = None
    cloudflare_access_policy: CloudflarePolicyDeletionClient | None = None
    cloudflare_access_application: CloudflareApplicationDeletionClient | None = None
    cloudflare_access_identity_provider: CloudflareIdentityProviderDeletionClient | None = None
    tailscale: TailscaleDeletionClient | None = None
    dokploy: DokployDeletionClient | None = None
    docker: DockerDeletionClient | None = None
