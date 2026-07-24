from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dokploy_wizard.lifecycle.changes import LifecyclePlan
from dokploy_wizard.lifecycle.engine import _task1_cloudflare_journal_for_lifecycle
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    opaque_hash,
    operation_key,
    snapshot_value_hash,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
    Task1CloudflareJournalIntent,
    Task1CloudflareJournalOperation,
)
from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1

__all__ = ("opaque_hash",)


class _InjectedCrash(RuntimeError):
    pass


def _raise_for(
    kind: JournalOperationKind,
) -> Callable[[Task1CloudflareJournalOperation], None]:
    raised = False

    def _raise(operation: Task1CloudflareJournalOperation) -> None:
        nonlocal raised
        if not raised and operation.intent.kind is kind:
            raised = True
            raise _InjectedCrash()

    return _raise


def _context(tmp_path: Path) -> Task1ProofContextV1:
    source_values = {"ROOT_DOMAIN": "example.com", "PACKS": "coder"}
    source_bytes = b"PACKS=coder\nROOT_DOMAIN=example.com\n"
    return derive_task1_proof_context(
        source_values=source_values,
        source_bytes=source_bytes,
        source_path=tmp_path / ".install-min.env",
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    ).context


def _lifecycle_plan(*, mode: str, phases_to_run: tuple[str, ...]) -> LifecyclePlan:
    return LifecyclePlan(
        mode=mode,
        reasons=(),
        applicable_phases=("preflight", "networking"),
        phases_to_run=phases_to_run,
        preserved_phases=(),
        initial_completed_steps=(),
        start_phase=None,
        raw_equivalent=False,
        desired_equivalent=False,
    )


def _journal_for_lifecycle(
    *,
    state_dir: Path,
    dry_run: bool,
    lifecycle_plan: LifecyclePlan,
    proof_context: Task1ProofContextV1,
) -> Task1CloudflareJournal | None:
    return _task1_cloudflare_journal_for_lifecycle(
        state_dir=state_dir,
        dry_run=dry_run,
        lifecycle_plan=lifecycle_plan,
        proof_context=proof_context,
    )


def _intent(kind: JournalOperationKind, scope: dict[str, str]) -> Task1CloudflareJournalIntent:
    name = scope.get("name", scope.get("hostname"))
    domain = scope.get("hostname")
    return Task1CloudflareJournalIntent(
        logical_key=operation_key(kind, scope),
        kind=kind,
        account_id=scope["account_id"],
        zone_id=scope.get("zone_id"),
        parent_id=scope.get("app_id"),
        expected_name_sha256=None if name is None else snapshot_value_hash(name),
        expected_domain_sha256=None if domain is None else snapshot_value_hash(domain),
        pre_absence_sha256=opaque_hash({"scope": scope}),
        desired_spec_sha256=opaque_hash({"scope": scope, "state": "desired"}),
    )
