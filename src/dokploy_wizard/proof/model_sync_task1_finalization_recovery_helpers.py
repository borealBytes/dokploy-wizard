"""Task 1 source restoration and process-claim recovery helpers."""

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_task1_evidence import (
    recover_task1_receipt_source,
    verify_task1_finalized_evidence,
)


def restore_bound_source(recovery: proof.ProofRecovery) -> proof.EnvReceipt:
    """Restore and revalidate the context-bound source before local-only terminal work."""
    guard = state.read_abort_guard(recovery.paths.guard_path)
    receipt = guard.env_receipt
    if receipt is None or receipt.context_evidence is None:
        raise proof.AbortGuardError("Task 1 finalization guard lacks context evidence")
    match guard.phase:
        case "proof_active":
            restored_evidence = recover_task1_receipt_source(
                evidence=receipt.context_evidence,
                source_path=Path(receipt.env_path),
                backup_path=Path(receipt.backup_path),
            )
            state.record_env_restored(
                recovery.paths.guard_path,
                claim_token=claim_token(recovery),
                receipt=replace(receipt, context_evidence=restored_evidence),
            )
            receipt = state.read_abort_guard(recovery.paths.guard_path).env_receipt
        case "env_restored":
            pass
        case unexpected:
            raise proof.AbortGuardError(
                f"Task 1 finalization cannot restore from guard phase {unexpected}"
            )
    if receipt is None or receipt.context_evidence is None:
        raise proof.AbortGuardError("Task 1 finalization lost restored context evidence")
    verify_task1_finalized_evidence(
        evidence=receipt.context_evidence,
        source_path=Path(receipt.env_path),
        backup_path=Path(receipt.backup_path),
    )
    return receipt


def reclaim_task1_recovery(
    paths: proof.ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> proof.ProofRecovery:
    """Claim a dead Task 1 finalization process for one local-only terminal action."""
    phase = state.read_abort_guard(paths.guard_path).phase
    match phase:
        case "proof_active" | "env_restored":
            return _reclaim_pre_finalize(paths, pid, start_time_ticks)
        case "finalize_intent":
            return _reclaim_finalize_intent(paths, pid, start_time_ticks)
        case unexpected:
            raise proof.AbortGuardError(
                f"Task 1 finalization cannot reclaim guard phase {unexpected}"
            )


def _reclaim_pre_finalize(
    paths: proof.ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> proof.ProofRecovery:
    token = secrets.token_urlsafe(24)
    state.reclaim_task1_finalization(
        paths.guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
        process_identity=state.process_identity_matches,
    )
    return proof.ProofRecovery(paths, proof.GuardClaim(token, pid, start_time_ticks), False, False)


def _reclaim_finalize_intent(
    paths: proof.ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> proof.ProofRecovery:
    token = secrets.token_urlsafe(24)
    state.reclaim_finalize_intent(
        paths.guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
        process_identity=state.process_identity_matches,
    )
    return proof.ProofRecovery(paths, proof.GuardClaim(token, pid, start_time_ticks), False, True)


def claim_token(recovery: proof.ProofRecovery) -> str:
    """Return the required process token for a claimed recovery operation."""
    if recovery.claim is None:
        raise proof.AbortGuardError("Task 1 recovery claim is absent")
    return recovery.claim.token
