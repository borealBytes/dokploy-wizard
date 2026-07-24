from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareError, CloudflareTunnel
from dokploy_wizard.networking.planner import _resolve_tunnel
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    JournalOperationState,
    Task1CloudflareJournal,
    Task1CloudflareJournalHooks,
)
from dokploy_wizard.proof.model_sync_task1_context import activate_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from dokploy_wizard.state import OwnershipLedger
from tests.unit._model_sync_task1_cloudflare_journal_common import (
    _context,
    _InjectedCrash,
    _raise_for,
)
from tests.unit._model_sync_task1_cloudflare_journal_mutation_backend import _MutationBackend
from tests.unit._model_sync_task1_cloudflare_journal_reconciliation_support import (
    _run_for,
    _run_tunnel,
)


def test_context_active_real_create_requires_journal_before_provider_call(tmp_path: Path) -> None:
    context = _context(tmp_path)
    backend = _MutationBackend()

    with (
        activate_task1_proof_context(context),
        pytest.raises(Task1ProofContextError, match="journal"),
    ):
        _resolve_tunnel(
            dry_run=False,
            account_id="account",
            tunnel_name="task1-tunnel",
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
            create_only=True,
        )

    assert backend.tunnel_creates == 0


def test_tunnel_crash_before_call_retries_once_after_zero_match(tmp_path: Path) -> None:
    backend = _MutationBackend()
    hooks = Task1CloudflareJournalHooks(after_intent_fsync=_raise_for(JournalOperationKind.TUNNEL))
    journal = Task1CloudflareJournal.for_context(
        state_dir=tmp_path, context=_context(tmp_path), hooks=hooks
    )

    with pytest.raises(_InjectedCrash):
        _resolve_tunnel(
            dry_run=False,
            account_id="account",
            tunnel_name="task1-tunnel",
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
            create_only=True,
            journal=journal,
        )

    assert backend.tunnel_creates == 0
    _resolve_tunnel(
        dry_run=False,
        account_id="account",
        tunnel_name="task1-tunnel",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=Task1CloudflareJournal.for_context(state_dir=tmp_path, context=_context(tmp_path)),
    )
    assert backend.tunnel_creates == 1


def test_tunnel_crash_after_response_recovers_exact_match_without_second_create(
    tmp_path: Path,
) -> None:
    backend = _MutationBackend()
    journal = Task1CloudflareJournal.for_context(
        state_dir=tmp_path,
        context=_context(tmp_path),
        hooks=Task1CloudflareJournalHooks(
            after_provider_response=_raise_for(JournalOperationKind.TUNNEL)
        ),
    )

    with pytest.raises(_InjectedCrash):
        _resolve_tunnel(
            dry_run=False,
            account_id="account",
            tunnel_name="task1-tunnel",
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
            create_only=True,
            journal=journal,
        )

    _resolve_tunnel(
        dry_run=False,
        account_id="account",
        tunnel_name="task1-tunnel",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=Task1CloudflareJournal.for_context(state_dir=tmp_path, context=_context(tmp_path)),
    )
    assert backend.tunnel_creates == 1


def test_intent_recovers_one_exact_match_and_rejects_multiple_matches(tmp_path: Path) -> None:
    context = _context(tmp_path)
    backend = _MutationBackend()
    journal = Task1CloudflareJournal.for_context(
        state_dir=tmp_path,
        context=context,
        hooks=Task1CloudflareJournalHooks(
            after_intent_fsync=_raise_for(JournalOperationKind.TUNNEL)
        ),
    )

    with pytest.raises(_InjectedCrash):
        _run_tunnel(journal, backend)
    backend.tunnels["tunnel-foreign"] = CloudflareTunnel("tunnel-foreign", "task1-tunnel")

    _run_tunnel(Task1CloudflareJournal.for_context(state_dir=tmp_path, context=context), backend)
    assert backend.tunnel_creates == 0

    multiple_dir = tmp_path / "multiple"
    multiple_backend = _MutationBackend()
    multiple_journal = Task1CloudflareJournal.for_context(
        state_dir=multiple_dir,
        context=context,
        hooks=Task1CloudflareJournalHooks(
            after_intent_fsync=_raise_for(JournalOperationKind.TUNNEL)
        ),
    )
    with pytest.raises(_InjectedCrash):
        _run_tunnel(multiple_journal, multiple_backend)
    multiple_backend.tunnels["tunnel-one"] = CloudflareTunnel("tunnel-one", "task1-tunnel")
    multiple_backend.tunnels["tunnel-two"] = CloudflareTunnel("tunnel-two", "task1-tunnel")

    with pytest.raises(CloudflareError, match="collision"):
        _run_tunnel(
            Task1CloudflareJournal.for_context(state_dir=multiple_dir, context=context),
            multiple_backend,
        )


def test_response_fingerprint_drift_keeps_intent_unowned(tmp_path: Path) -> None:
    backend = _MutationBackend(drift_tunnel_response=True)
    journal = Task1CloudflareJournal.for_context(state_dir=tmp_path, context=_context(tmp_path))

    with pytest.raises(CloudflareError, match="response drifted"):
        _run_tunnel(journal, backend)

    assert journal.document().operations[0].state is JournalOperationState.INTENT


@pytest.mark.parametrize(
    "kind",
    (
        JournalOperationKind.ACCESS_APPLICATION,
        JournalOperationKind.ACCESS_POLICY,
        JournalOperationKind.DNS_RECORD,
        JournalOperationKind.TUNNEL_CONFIGURATION,
    ),
)
def test_each_remaining_provider_response_boundary_resumes_without_duplicate_write(
    tmp_path: Path,
    kind: JournalOperationKind,
) -> None:
    backend = _MutationBackend()
    context = _context(tmp_path)
    run = _run_for(kind)
    journal = Task1CloudflareJournal.for_context(
        state_dir=tmp_path,
        context=context,
        hooks=Task1CloudflareJournalHooks(after_provider_response=_raise_for(kind)),
    )

    with pytest.raises(_InjectedCrash):
        run(journal, backend)

    run(Task1CloudflareJournal.for_context(state_dir=tmp_path, context=context), backend)
    assert backend.create_count(kind) == 1
