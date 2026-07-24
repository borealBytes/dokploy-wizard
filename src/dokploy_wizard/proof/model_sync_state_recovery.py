"""Recovery-only GuardV3 transitions kept separate from normal state mutation."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_state as state


def reclaim_finalize_intent(
    path: Path,
    *,
    pid: int,
    start_time_ticks: str,
    claim_token: str,
    process_identity: Callable[[int, str], bool],
) -> proof.AbortGuard:
    guard = state.read_abort_guard(path)
    if (
        guard.phase != "finalize_intent"
        or guard.claimant_kind != "process"
        or guard.pid is None
        or guard.start_time_ticks is None
        or process_identity(guard.pid, guard.start_time_ticks)
    ):
        raise proof.AbortGuardError("finalization guard is not reclaimable")
    if not proof.valid_process_identity(pid, start_time_ticks, claim_token):
        raise proof.AbortGuardError("abort guard process identity is invalid")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            guard.phase,
            "process",
            pid,
            start_time_ticks,
            claim_token,
            guard.env_receipt,
            guard.attestation,
        ),
    )


def reclaim_task1_finalization(
    path: Path,
    *,
    pid: int,
    start_time_ticks: str,
    claim_token: str,
    process_identity: Callable[[int, str], bool],
) -> proof.AbortGuard:
    """Adopt a dead pre-finalization process without resetting its recovery evidence."""
    guard = state.read_abort_guard(path)
    if (
        guard.phase not in {"proof_active", "env_restored"}
        or guard.claimant_kind != "process"
        or guard.pid is None
        or guard.start_time_ticks is None
        or process_identity(guard.pid, guard.start_time_ticks)
    ):
        raise proof.AbortGuardError("Task 1 finalization guard is not reclaimable")
    if not proof.valid_process_identity(pid, start_time_ticks, claim_token):
        raise proof.AbortGuardError("abort guard process identity is invalid")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            guard.phase,
            "process",
            pid,
            start_time_ticks,
            claim_token,
            guard.env_receipt,
            None,
        ),
    )


def reset_abort_guard(path: Path) -> proof.AbortGuard:
    guard = state.read_abort_guard(path)
    if guard.phase != "rollback" or guard.claimant_kind != "plan":
        raise proof.AbortGuardError("only a plan-owned rollback guard may be reset")
    fresh = proof.AbortGuard(
        secrets.token_hex(32), "armed", "ready", "plan", None, None, None, None, None
    )
    return state._write_guard(path, fresh)


def recover_dead_abort_claim(path: Path, *, process_identity: Callable[[int, str], bool]) -> bool:
    guard = state.read_abort_guard(path)
    if guard.claimant_kind == "plan":
        return False
    if guard.pid is None or guard.start_time_ticks is None:
        raise proof.AbortGuardError("process abort guard lacks process identity")
    if process_identity(guard.pid, guard.start_time_ticks):
        return False
    state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            "rollback",
            "plan",
            None,
            None,
            None,
            guard.env_receipt,
            guard.attestation,
        ),
    )
    return True


def disarm_abort_guard(path: Path) -> proof.AbortGuard:
    guard = state.read_abort_guard(path)
    if guard.phase != "complete" or guard.claimant_kind != "plan":
        raise proof.AbortGuardError("only a complete plan guard may be disarmed")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "disarmed",
            "complete",
            "plan",
            None,
            None,
            None,
            guard.env_receipt,
            guard.attestation,
        ),
    )


def record_finalize_intent(
    path: Path, *, claim_token: str, attestation: proof.BaselineAttestation
) -> proof.AbortGuard:
    try:
        proof.validate_attestation(attestation)
    except ValueError as error:
        raise proof.AbortGuardError("attestation snapshot evidence is invalid") from error
    guard = state._claimed_guard(path, claim_token, ("proof_active", "env_restored"))
    if (
        guard.env_receipt is None
        or guard.guard_id != attestation.guard_id
        or proof.receipt_identity(guard.env_receipt)
        != proof.receipt_identity(attestation.env_receipt)
        or guard.env_receipt.context_evidence != attestation.context_evidence
        or (
            guard.env_receipt.context_evidence is not None
            and (
                guard.phase != "env_restored"
                or guard.env_receipt.context_evidence.observed_restored_source_sha256 is None
            )
        )
    ):
        raise proof.AbortGuardError("attestation does not bind the active guard receipt")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            "finalize_intent",
            "process",
            guard.pid,
            guard.start_time_ticks,
            claim_token,
            guard.env_receipt,
            attestation,
        ),
    )


def complete_abort_guard(path: Path, *, claim_token: str) -> proof.AbortGuard:
    guard = state._claimed_guard(path, claim_token, "finalize_intent")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            "complete",
            "plan",
            None,
            None,
            None,
            guard.env_receipt,
            guard.attestation,
        ),
    )


def begin_rollback(path: Path, *, claim_token: str) -> proof.AbortGuard:
    guard = state.read_abort_guard(path)
    if (
        guard.claimant_kind != "process"
        or guard.claim_token != claim_token
        or guard.phase == "complete"
    ):
        raise proof.AbortGuardError("abort guard claim token does not authorize rollback")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            "rollback",
            "process",
            guard.pid,
            guard.start_time_ticks,
            claim_token,
            guard.env_receipt,
            guard.attestation,
        ),
    )


def transfer_abort_guard_to_plan(path: Path, *, claim_token: str) -> proof.AbortGuard:
    guard = state._claimed_guard(path, claim_token, "rollback")
    return state._write_guard(
        path,
        proof.AbortGuard(
            guard.guard_id,
            "armed",
            "rollback",
            "plan",
            None,
            None,
            None,
            guard.env_receipt,
            guard.attestation,
        ),
    )
