from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareAccessApplication
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    application_fingerprint,
    dns_fingerprint,
    policy_fingerprint,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
    Task1CloudflareJournalHooks,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import (
    _context,
    _InjectedCrash,
    _intent,
    _journal_for_lifecycle,
    _lifecycle_plan,
    _raise_for,
)


def test_cleanup_reverses_only_journaled_task1_resources(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    fixture = CleanupBackend()
    operations = (
        (
            JournalOperationKind.TUNNEL,
            {"account_id": "account", "name": "task1-tunnel"},
            "tunnel-1",
            tunnel_fingerprint(fixture.tunnels["tunnel-1"]),
        ),
        (
            JournalOperationKind.DNS_RECORD,
            {"account_id": "account", "zone_id": "zone", "hostname": "dp.example.com"},
            "dns-1",
            dns_fingerprint(fixture.records["dns-1"]),
        ),
        (
            JournalOperationKind.ACCESS_APPLICATION,
            {"account_id": "account", "hostname": "dp.example.com"},
            "app-1",
            application_fingerprint(fixture.apps["app-1"]),
        ),
        (
            JournalOperationKind.ACCESS_POLICY,
            {"account_id": "account", "app_id": "app-1", "hostname": "dp.example.com"},
            "policy-1",
            policy_fingerprint(fixture.policies["policy-1"]),
        ),
    )
    for kind, scope, resource_id, fingerprint in operations:
        operation = journal.record_intent(_intent(kind, scope))
        journal.checkpoint_created(
            operation,
            response_id=resource_id,
            post_fingerprint_sha256=fingerprint,
        )

    backend = CleanupBackend()
    receipt = journal.cleanup(backend)

    assert backend.deleted == ["policy:policy-1", "app:app-1", "dns:dns-1", "tunnel:tunnel-1"]
    assert receipt["status"] == "cleaned"
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert journal.path.with_suffix(".receipt.json").stat().st_mode & 0o777 == 0o600


def test_cleanup_rejects_interrupted_create_intent(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1-tunnel"})
    )

    with pytest.raises(Task1ProofContextError, match="incomplete intent"):
        journal.cleanup(CleanupBackend())


def test_lifecycle_defers_journal_for_dry_run_and_noop(tmp_path: Path) -> None:
    context = _context(tmp_path)
    mutation_plan = _lifecycle_plan(mode="apply", phases_to_run=("networking",))

    assert (
        _journal_for_lifecycle(
            state_dir=tmp_path,
            dry_run=True,
            lifecycle_plan=mutation_plan,
            proof_context=context,
        )
        is None
    )
    assert (
        _journal_for_lifecycle(
            state_dir=tmp_path,
            dry_run=False,
            lifecycle_plan=_lifecycle_plan(mode="noop", phases_to_run=("networking",)),
            proof_context=context,
        )
        is None
    )
    deferred = _journal_for_lifecycle(
        state_dir=tmp_path,
        dry_run=False,
        lifecycle_plan=mutation_plan,
        proof_context=context,
    )

    assert deferred is not None
    assert not deferred.path.exists()


def test_cleanup_resumes_after_checkpoint_interruption_and_preserves_foreign_drift(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    backend = CleanupBackend()
    journal = Task1CloudflareJournal.open(
        state_dir=tmp_path,
        context=context,
        hooks=Task1CloudflareJournalHooks(
            after_cleanup_checkpoint=_raise_for(JournalOperationKind.ACCESS_APPLICATION)
        ),
    )
    operation = journal.record_intent(
        _intent(
            JournalOperationKind.ACCESS_APPLICATION,
            {"account_id": "account", "hostname": "dp.example.com"},
        )
    )
    journal.checkpoint_created(
        operation,
        response_id="app-1",
        post_fingerprint_sha256=application_fingerprint(backend.apps["app-1"]),
    )

    with pytest.raises(_InjectedCrash):
        journal.cleanup(backend)

    assert backend.deleted == ["app:app-1"]
    Task1CloudflareJournal.open(state_dir=tmp_path, context=context).cleanup(backend)
    assert backend.deleted == ["app:app-1"]

    foreign_dir = tmp_path / "foreign"
    foreign_backend = CleanupBackend()
    foreign_journal = Task1CloudflareJournal.open(state_dir=foreign_dir, context=context)
    foreign_operation = foreign_journal.record_intent(
        _intent(
            JournalOperationKind.ACCESS_APPLICATION,
            {"account_id": "account", "hostname": "dp.example.com"},
        )
    )
    foreign_journal.checkpoint_created(
        foreign_operation,
        response_id="app-1",
        post_fingerprint_sha256=application_fingerprint(foreign_backend.apps["app-1"]),
    )
    foreign_backend.apps["app-1"] = CloudflareAccessApplication(
        "app-1", "foreign", "dp.example.com", "self_hosted", ("otp",)
    )

    with pytest.raises(Task1ProofContextError, match="drifted foreign"):
        foreign_journal.cleanup(foreign_backend)

    assert foreign_backend.deleted == []
