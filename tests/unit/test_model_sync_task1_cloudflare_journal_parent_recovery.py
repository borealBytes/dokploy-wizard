from __future__ import annotations

from pathlib import Path

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
from dokploy_wizard.proof.model_sync_task1_cloudflare_intent_reconciliation import (
    reconcile_incomplete_intents,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationState,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotPage,
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
    CloudflareSnapshotCollectionError,
)
from tests.unit._model_sync_task1_cloudflare_journal_common import _context
from tests.unit._model_sync_task1_cloudflare_journal_crash_safety_support import (
    _configuration_intent,
)


class _MissingParentInventory:
    def list_tunnels_page(
        self, _account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareTunnel]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def list_dns_records_page(
        self, _zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareDnsRecord]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def list_access_applications_page(
        self, _account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessApplication]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def list_access_policies_page(
        self, _account_id: str, _app_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessPolicy]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def list_identity_providers_page(
        self, _account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessIdentityProvider]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def list_certificate_packs_page(
        self, _zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareCertificatePack]:
        return CloudflareSnapshotPage(page, per_page, 0, 1, ())

    def get_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str
    ) -> tuple[dict[str, JsonValue], ...]:
        return ({"service": "pre"},)


def test_inventory_reconciliation_rejects_missing_configuration_parent(tmp_path: Path) -> None:
    # Given
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    operation = journal.record_intent(
        _configuration_intent(({"service": "pre"},), ({"service": "desired"},))
    )
    scope = CloudflareSnapshotScope("f" * 64, "account", "zone")

    # When / Then
    with pytest.raises(CloudflareSnapshotCollectionError, match="parent is missing"):
        reconcile_incomplete_intents(
            journal=journal,
            backend=_MissingParentInventory(),
            scope=scope,
        )

    assert journal.document().operations == (operation,)
    assert operation.state is JournalOperationState.INTENT
