from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TypeVar

import pytest

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
    CloudflareSnapshotCollectionError,
    CloudflareSnapshotPage,
    CloudflareSnapshotScope,
    capture_cloudflare_snapshot,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _SnapshotBackend:
    duplicate_tunnel: bool = False
    changing_total: bool = False
    certificate_hosts: tuple[str, ...] = ("label-host",)

    def list_tunnels_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareTunnel]:
        del account_id, per_page
        item = CloudflareTunnel("tunnel-a", "label-tunnel", "cloudflare", "healthy")
        if self.duplicate_tunnel or self.changing_total:
            total = 3 if self.changing_total and page == 2 else 2
            return CloudflareSnapshotPage(page, 100, total, 2, (item,))
        return CloudflareSnapshotPage(page, 100, 1, 1, (item,) if page == 1 else ())

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, JsonValue], ...]:
        del account_id, tunnel_id
        return ({"hostname": "label-ingress", "service": "value-ingress"},)

    def list_dns_records_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareDnsRecord]:
        del zone_id, per_page
        return _page(
            page,
            CloudflareDnsRecord("dns-a", "label-dns", "CNAME", "value-dns", True),
        )

    def list_identity_providers_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessIdentityProvider]:
        del account_id, per_page
        return _page(
            page,
            CloudflareAccessIdentityProvider("otp-a", "One-time PIN login", "onetimepin"),
        )

    def list_access_applications_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessApplication]:
        del account_id, per_page
        return _page(
            page,
            CloudflareAccessApplication(
                "app-a", "label-app", "label-domain", "self_hosted", ("otp-a",)
            ),
        )

    def list_access_policies_page(
        self, account_id: str, app_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessPolicy]:
        del account_id, app_id, per_page
        return _page(
            page, CloudflareAccessPolicy("policy-a", "app-a", "label-policy", "allow", ("email-a",))
        )

    def list_certificate_packs_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareCertificatePack]:
        del zone_id, per_page
        return _page(
            page, CloudflareCertificatePack("cert-a", "advanced", "active", self.certificate_hosts)
        )


def _page(page: int, item: T) -> CloudflareSnapshotPage[T]:
    return CloudflareSnapshotPage(page, 100, 1, 1, (item,) if page == 1 else ())


def _scope() -> CloudflareSnapshotScope:
    return CloudflareSnapshotScope("1" * 64, "account-a", "zone-a")


def test_capture_is_complete_redacted_and_canonical_when_pages_are_valid() -> None:
    # Given: a narrow fake that exposes all six complete Cloudflare planes.
    backend = _SnapshotBackend()

    # When: the collector captures one context-bound inventory.
    snapshot = capture_cloudflare_snapshot(backend, _scope())

    # Then: each plane is present and all sensitive values are only hashed.
    assert [entry.kind for entry in snapshot.collections] == [
        "access_application",
        "access_policy",
        "certificate_pack",
        "dns_record",
        "identity_provider",
        "tunnel",
    ]
    serialized = snapshot.to_bytes().decode()
    assert "label-" not in serialized
    assert "value-" not in serialized
    assert "email-a" not in serialized


@pytest.mark.parametrize(
    "backend",
    [
        _SnapshotBackend(duplicate_tunnel=True),
        _SnapshotBackend(changing_total=True),
    ],
)
def test_capture_rejects_incomplete_or_changing_tunnel_pagination_when_observed(
    backend: _SnapshotBackend,
) -> None:
    # Given: a tunnel collection violating the stable complete-page contract.

    # When / Then: no partial inventory is emitted.
    with pytest.raises(CloudflareSnapshotCollectionError):
        capture_cloudflare_snapshot(backend, _scope())


def test_capture_canonicalizes_certificate_hashes_after_host_projection() -> None:
    backend = _SnapshotBackend(certificate_hosts=("z.example", "a.example"))

    snapshot = capture_cloudflare_snapshot(backend, _scope())

    certificate = next(item for item in snapshot.resources if item.kind == "certificate_pack")
    expected = sorted(
        {hashlib.sha256(value.encode()).hexdigest() for value in backend.certificate_hosts}
    )
    assert certificate.payload["host_sha256"] == expected
