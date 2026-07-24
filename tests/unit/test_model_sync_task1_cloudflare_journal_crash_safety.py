from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareError
from dokploy_wizard.networking.planner import _reconcile_task1_tunnel_configuration
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _InjectedCrash, _intent
from tests.unit._model_sync_task1_cloudflare_journal_crash_safety_support import (
    _configuration_intent,
    _ConfigurationBackend,
    _record_tunnel_and_configuration,
    _TunnelCleanupBackend,
)


def test_tunnel_configuration_intent_rejects_third_party_drift_before_update(
    tmp_path: Path,
) -> None:
    pre_image: tuple[dict[str, object], ...] = ({"service": "https://configuration-a"},)
    unrelated: tuple[dict[str, object], ...] = ({"service": "https://configuration-b"},)
    desired: tuple[dict[str, object], ...] = ({"service": "https://configuration-c"},)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    intent = _configuration_intent(pre_image, desired)
    operation = journal.record_intent(intent)
    backend = _ConfigurationBackend(unrelated)

    with pytest.raises(CloudflareError, match="recovery drifted"):
        _reconcile_task1_tunnel_configuration(
            journal=journal,
            backend=backend,
            account_id="account",
            tunnel_id="tunnel-1",
            ingress=desired,
        )

    assert backend.updates == 0
    assert journal.document().operations == (operation,)


def test_cleanup_rejects_empty_active_journal_without_receipt(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    receipt = journal.path.with_suffix(".receipt.json")

    with pytest.raises(Task1ProofContextError, match="no operations"):
        journal.cleanup(_TunnelCleanupBackend(crash_after_delete=False))

    assert not receipt.exists()


def test_cleanup_accepts_a_durably_non_created_provider_intent(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    operation = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1-tunnel"})
    )
    journal.checkpoint_aborted(operation)

    receipt = journal.cleanup(CleanupBackend())

    assert receipt["status"] == "restored"
    assert journal.document().status.value == "restored"


def test_cleanup_preserves_unknown_receipt_bytes(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    backend = _TunnelCleanupBackend(crash_after_delete=False)
    _record_tunnel_and_configuration(journal, backend)
    receipt = journal.path.with_suffix(".receipt.json")
    receipt.write_bytes(b"foreign receipt\n")
    receipt.chmod(0o600)

    with pytest.raises(Task1ProofContextError, match="receipt"):
        journal.cleanup(backend)

    assert receipt.read_bytes() == b"foreign receipt\n"


def test_cleanup_resumes_when_tunnel_delete_crashes_before_checkpoint(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    backend = _TunnelCleanupBackend(crash_after_delete=True)
    _record_tunnel_and_configuration(journal, backend)

    with pytest.raises(_InjectedCrash):
        journal.cleanup(backend)

    receipt = Task1CloudflareJournal.for_context(
        state_dir=tmp_path, context=_context(tmp_path)
    ).cleanup(backend)

    assert receipt["status"] == "cleaned"
