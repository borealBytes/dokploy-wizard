from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import tunnel_fingerprint
from dokploy_wizard.proof.model_sync_task1_cloudflare_intent_reconciliation import (
    _resolve_resources,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    JournalOperationState,
    JournalStatus,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
    CloudflareSnapshotCollectionError,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent


def _backend_snapshot(backend: CleanupBackend) -> str:
    return repr(
        (
            tuple(sorted(backend.tunnels.items())),
            tuple(sorted(backend.records.items())),
            tuple(sorted(backend.apps.items())),
            tuple(sorted(backend.policies.items())),
        )
    )


def _record_created_tunnel(journal: Task1CloudflareJournal, backend: CleanupBackend) -> None:
    operation = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1-tunnel"})
    )
    journal.checkpoint_created(
        operation,
        response_id="tunnel-1",
        post_fingerprint_sha256=tunnel_fingerprint(backend.tunnels["tunnel-1"]),
    )


def test_mixed_cleanup_restores_created_resource_after_verified_noncreation(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=context)
    backend = CleanupBackend()
    created_tunnel = backend.tunnels.pop("tunnel-1")
    pre_snapshot = _backend_snapshot(backend)
    backend.tunnels["tunnel-1"] = created_tunnel
    _record_created_tunnel(journal, backend)
    absent = journal.record_intent(
        _intent(
            JournalOperationKind.DNS_RECORD,
            {"account_id": "account", "zone_id": "zone", "hostname": "absent.example.com"},
        )
    )

    # When
    _resolve_resources(journal, absent, ())
    first_receipt = journal.cleanup(backend)
    first_receipt_bytes = journal.path.with_suffix(".receipt.json").read_bytes()
    second_receipt = Task1CloudflareJournal.for_context(
        state_dir=tmp_path, context=context
    ).cleanup(backend)

    # Then
    document = journal.document()
    assert tuple(operation.state for operation in document.operations) == (
        JournalOperationState.CLEANED,
        JournalOperationState.ABORTED,
    )
    assert document.status is JournalStatus.RESTORED
    assert _backend_snapshot(backend) == pre_snapshot
    assert backend.deleted == ["tunnel:tunnel-1"]
    assert first_receipt == second_receipt
    assert journal.path.with_suffix(".receipt.json").read_bytes() == first_receipt_bytes


@pytest.mark.parametrize(
    "candidates",
    (
        (("dns-1", "c" * 64),),
        (("dns-1", "d" * 64), ("dns-2", "d" * 64)),
    ),
)
def test_failed_intent_reconciliation_blocks_cleanup_of_earlier_creation(
    tmp_path: Path, candidates: tuple[tuple[str, str], ...]
) -> None:
    # Given
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    backend = CleanupBackend()
    _record_created_tunnel(journal, backend)
    unresolved = journal.record_intent(
        _intent(
            JournalOperationKind.DNS_RECORD,
            {"account_id": "account", "zone_id": "zone", "hostname": "dns.example.com"},
        )
    )

    # When / Then
    with pytest.raises(CloudflareSnapshotCollectionError):
        _resolve_resources(journal, unresolved, candidates)
    with pytest.raises(Task1ProofContextError, match="incomplete intent"):
        journal.cleanup(backend)

    document = journal.document()
    assert tuple(operation.state for operation in document.operations) == (
        JournalOperationState.CREATED,
        JournalOperationState.INTENT,
    )
    assert document.status is JournalStatus.ACTIVE
    assert backend.deleted == []
