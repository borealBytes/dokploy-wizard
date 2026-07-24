"""Bounded complete Cloudflare collection for Task 1 snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TypeVar

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotBackend,
    CloudflareSnapshotPage,
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_projection import (
    project_resources,
    select_otp,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotCollectionV1,
    CloudflareSnapshotError,
    CloudflareSnapshotV1,
)

_PAGE_SIZE = 100
_MAX_PAGES = 100
T = TypeVar("T")

__all__ = (
    "CloudflareSnapshotCollectionError",
    "CloudflareSnapshotPage",
    "CloudflareSnapshotScope",
    "capture_cloudflare_snapshot",
)


class CloudflareSnapshotCollectionError(CloudflareSnapshotError):
    """Raised when complete Cloudflare pagination cannot be proven."""


def capture_cloudflare_snapshot(
    backend: CloudflareSnapshotBackend, scope: CloudflareSnapshotScope
) -> CloudflareSnapshotV1:
    """Capture every relevant provider plane through bounded stable pagination."""
    tunnels, tunnel_meta = _pages(
        "tunnel",
        lambda page: backend.list_tunnels_page(scope.account_id, page, _PAGE_SIZE),
        lambda item: item.tunnel_id,
    )
    dns, dns_meta = _pages(
        "dns_record",
        lambda page: backend.list_dns_records_page(scope.zone_id, page, _PAGE_SIZE),
        lambda item: item.record_id,
    )
    providers, provider_meta = _pages(
        "identity_provider",
        lambda page: backend.list_identity_providers_page(scope.account_id, page, _PAGE_SIZE),
        lambda item: item.provider_id,
    )
    apps, app_meta = _pages(
        "access_application",
        lambda page: backend.list_access_applications_page(scope.account_id, page, _PAGE_SIZE),
        lambda item: item.app_id,
    )
    certificates, certificate_meta = _pages(
        "certificate_pack",
        lambda page: backend.list_certificate_packs_page(scope.zone_id, page, _PAGE_SIZE),
        lambda item: item.pack_id,
    )
    policies, policy_meta = _all_policies(backend, scope.account_id, apps)
    otp = select_otp(providers)
    return CloudflareSnapshotV1.create(
        context_sha256=scope.context_sha256,
        account_id_sha256=hashlib.sha256(scope.account_id.encode()).hexdigest(),
        zone_id_sha256=hashlib.sha256(scope.zone_id.encode()).hexdigest(),
        collections=(tunnel_meta, dns_meta, provider_meta, app_meta, policy_meta, certificate_meta),
        resources=project_resources(
            backend, scope.account_id, tunnels, dns, providers, apps, policies, certificates
        ),
        otp_provider_sha256=otp.semantic_sha256,
    )


def _pages(
    kind: str, fetch: Callable[[int], CloudflareSnapshotPage[T]], identifier: Callable[[T], str]
) -> tuple[tuple[T, ...], CloudflareSnapshotCollectionV1]:
    first = fetch(1)
    _validate_page(first, expected_page=1, expected_total=None)
    expected_pages = max(1, (first.total_count + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if first.total_pages != expected_pages or expected_pages > _MAX_PAGES:
        raise CloudflareSnapshotCollectionError("Cloudflare pagination is incomplete or unbounded")
    collected = list(first.items)
    for page_number in range(2, expected_pages + 1):
        page = fetch(page_number)
        _validate_page(page, expected_page=page_number, expected_total=first.total_count)
        if page.total_pages != expected_pages:
            raise CloudflareSnapshotCollectionError("Cloudflare pagination total pages changed")
        collected.extend(page.items)
    identities = tuple(identifier(item) for item in collected)
    if len(collected) != first.total_count or len(identities) != len(set(identities)):
        raise CloudflareSnapshotCollectionError("Cloudflare pagination is truncated or duplicated")
    return tuple(collected), CloudflareSnapshotCollectionV1(
        kind, expected_pages, _PAGE_SIZE, len(collected)
    )


def _validate_page(
    page: CloudflareSnapshotPage[T], *, expected_page: int, expected_total: int | None
) -> None:
    invalid = (
        page.page != expected_page
        or page.per_page != _PAGE_SIZE
        or isinstance(page.total_count, bool)
        or isinstance(page.total_pages, bool)
        or page.total_count < 0
        or page.total_pages < 1
        or len(page.items) > _PAGE_SIZE
        or (expected_total is not None and page.total_count != expected_total)
    )
    if invalid:
        raise CloudflareSnapshotCollectionError("Cloudflare pagination metadata is malformed")


def _all_policies(
    backend: CloudflareSnapshotBackend,
    account_id: str,
    apps: tuple[CloudflareAccessApplication, ...],
) -> tuple[tuple[CloudflareAccessPolicy, ...], CloudflareSnapshotCollectionV1]:
    policies: list[CloudflareAccessPolicy] = []
    page_count = 0
    for app in apps:
        items, metadata = _pages(
            "access_policy",
            lambda page: backend.list_access_policies_page(
                account_id, app.app_id, page, _PAGE_SIZE
            ),
            lambda item: item.policy_id,
        )
        if any(item.app_id != app.app_id for item in items):
            raise CloudflareSnapshotCollectionError("Cloudflare policy parent drifted")
        policies.extend(items)
        page_count += metadata.page_count
    if len({item.policy_id for item in policies}) != len(policies):
        raise CloudflareSnapshotCollectionError(
            "Cloudflare policies are duplicated across applications"
        )
    return tuple(policies), CloudflareSnapshotCollectionV1(
        "access_policy", page_count, _PAGE_SIZE, len(policies)
    )
