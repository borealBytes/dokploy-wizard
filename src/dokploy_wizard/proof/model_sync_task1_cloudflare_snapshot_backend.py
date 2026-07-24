"""Narrow complete-list backend contract for Cloudflare snapshot evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

if TYPE_CHECKING:
    from dokploy_wizard.networking.cloudflare import (
        CloudflareAccessApplication,
        CloudflareAccessIdentityProvider,
        CloudflareAccessPolicy,
        CloudflareCertificatePack,
        CloudflareDnsRecord,
        CloudflareTunnel,
    )

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CloudflareSnapshotScope:
    """Transient raw collection scope bound into the persisted snapshot by hash."""

    context_sha256: str
    account_id: str
    zone_id: str


@dataclass(frozen=True, slots=True)
class CloudflareSnapshotPage(Generic[T]):
    """One backend page whose result_info metadata remains independently checked."""

    page: int
    per_page: int
    total_count: int
    total_pages: int
    items: tuple[T, ...]


class CloudflareSnapshotBackend(Protocol):
    """Complete-list API surface used only for redacted Task 1 evidence."""

    def list_tunnels_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareTunnel]: ...

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    def list_dns_records_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareDnsRecord]: ...

    def list_identity_providers_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessIdentityProvider]: ...

    def list_access_applications_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessApplication]: ...

    def list_access_policies_page(
        self, account_id: str, app_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessPolicy]: ...

    def list_certificate_packs_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareCertificatePack]: ...
