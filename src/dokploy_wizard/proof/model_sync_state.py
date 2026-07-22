"""Strict GuardV3 journal for crash-safe Task 1 baseline finalization."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path

from dokploy_wizard.proof import (
    AbortGuard,
    AbortGuardError,
    BaselineAttestation,
    EnvReceipt,
    GuardPhase,
    abort_guard_payload,
    canonical_json_bytes,
    parse_abort_guard,
    process_identity_matches,
    process_start_time_ticks,
    read_guard_bytes,
    receipt_identity,
    valid_process_identity,
)
from dokploy_wizard.proof.model_sync_artifacts import atomic_write_bytes
from dokploy_wizard.proof.model_sync_results import validate_attestation

__all__ = (
    "AbortGuard",
    "AbortGuardError",
    "process_identity_matches",
    "process_start_time_ticks",
)


def arm_abort_guard(path: Path) -> AbortGuard:
    """Create one fresh plan-owned ready guard without adopting an existing path."""
    if os.path.lexists(path):
        raise AbortGuardError("existing abort guard must be reconciled before re-arming")
    guard = AbortGuard(
        secrets.token_hex(32), "armed", "ready", "plan", None, None, None, None, None
    )
    return _write_guard(path, guard)


def read_abort_guard(path: Path) -> AbortGuard:
    """Read only one bounded canonical mode-0600 regular GuardV3 file."""
    try:
        raw = read_guard_bytes(path)
    except (OSError, ValueError) as error:
        raise AbortGuardError("abort guard is unreadable") from error
    try:
        decoded = json.loads(raw)
        canonical = canonical_json_bytes(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        raise AbortGuardError("abort guard is unreadable") from error
    if raw != canonical + b"\n":
        raise AbortGuardError("abort guard bytes are not canonical")
    try:
        guard = parse_abort_guard(decoded)
        if guard.attestation is not None:
            validate_attestation(guard.attestation)
    except ValueError as error:
        raise AbortGuardError("abort guard schema or attestation is invalid") from error
    return guard


def claim_abort_guard(
    path: Path, *, pid: int, start_time_ticks: str, claim_token: str
) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "ready" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a ready plan guard may be claimed")
    if not valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    claimed = AbortGuard(
        guard.guard_id, "armed", "claimed", "process",
        pid, start_time_ticks, claim_token, None, None,
    )
    return _write_guard(path, claimed)


def claim_rollback_guard(
    path: Path, *, pid: int, start_time_ticks: str, claim_token: str
) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "rollback" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a plan-owned rollback guard may be claimed")
    if not valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    claimed = AbortGuard(
        guard.guard_id, "armed", "rollback", "process",
        pid, start_time_ticks, claim_token, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, claimed)


def reclaim_finalize_intent(
    path: Path,
    *,
    pid: int,
    start_time_ticks: str,
    claim_token: str,
    process_identity: Callable[[int, str], bool],
) -> AbortGuard:
    guard = read_abort_guard(path)
    if (
        guard.phase != "finalize_intent"
        or guard.claimant_kind != "process"
        or guard.pid is None
        or guard.start_time_ticks is None
        or process_identity(guard.pid, guard.start_time_ticks)
    ):
        raise AbortGuardError("finalization guard is not reclaimable")
    if not valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    reclaimed = AbortGuard(
        guard.guard_id, "armed", guard.phase, "process",
        pid, start_time_ticks, claim_token, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, reclaimed)


def record_env_intent(path: Path, *, claim_token: str, receipt: EnvReceipt) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "claimed")
    intended = AbortGuard(
        guard.guard_id, "armed", "env_intent", "process",
        guard.pid, guard.start_time_ticks, claim_token, receipt, None,
    )
    return _write_guard(path, intended)


def record_proof_active(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "env_intent")
    active = AbortGuard(
        guard.guard_id, "armed", "proof_active", "process",
        guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, None,
    )
    return _write_guard(path, active)


def record_finalize_intent(
    path: Path, *, claim_token: str, attestation: BaselineAttestation
) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "proof_active")
    if (
        guard.env_receipt is None
        or guard.guard_id != attestation.guard_id
        or receipt_identity(guard.env_receipt) != receipt_identity(attestation.env_receipt)
    ):
        raise AbortGuardError("attestation does not bind the active guard receipt")
    intent = AbortGuard(
        guard.guard_id, "armed", "finalize_intent", "process",
        guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, attestation,
    )
    return _write_guard(path, intent)


def complete_abort_guard(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "finalize_intent")
    complete = AbortGuard(
        guard.guard_id, "armed", "complete", "plan",
        None, None, None, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, complete)


def begin_rollback(path: Path, *, claim_token: str) -> AbortGuard:
    guard = read_abort_guard(path)
    if (
        guard.claimant_kind != "process"
        or guard.claim_token != claim_token
        or guard.phase == "complete"
    ):
        raise AbortGuardError("abort guard claim token does not authorize rollback")
    rollback = AbortGuard(
        guard.guard_id, "armed", "rollback", "process",
        guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, rollback)


def transfer_abort_guard_to_plan(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "rollback")
    plan = AbortGuard(
        guard.guard_id, "armed", "rollback", "plan",
        None, None, None, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, plan)


def reset_abort_guard(path: Path) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "rollback" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a plan-owned rollback guard may be reset")
    fresh = AbortGuard(
        secrets.token_hex(32), "armed", "ready", "plan", None, None, None, None, None
    )
    return _write_guard(path, fresh)


def recover_dead_abort_claim(
    path: Path, *, process_identity: Callable[[int, str], bool]
) -> bool:
    guard = read_abort_guard(path)
    if guard.claimant_kind == "plan":
        return False
    if guard.pid is None or guard.start_time_ticks is None:
        raise AbortGuardError("process abort guard lacks process identity")
    if process_identity(guard.pid, guard.start_time_ticks):
        return False
    recovered = AbortGuard(
        guard.guard_id, "armed", "rollback", "plan",
        None, None, None, guard.env_receipt, guard.attestation,
    )
    _write_guard(path, recovered)
    return True


def disarm_abort_guard(path: Path) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "complete" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a complete plan guard may be disarmed")
    disarmed = AbortGuard(
        guard.guard_id, "disarmed", "complete", "plan",
        None, None, None, guard.env_receipt, guard.attestation,
    )
    return _write_guard(path, disarmed)


def _claimed_guard(path: Path, claim_token: str, phase: GuardPhase) -> AbortGuard:
    guard = read_abort_guard(path)
    if (
        guard.phase != phase
        or guard.claimant_kind != "process"
        or guard.claim_token != claim_token
    ):
        raise AbortGuardError("abort guard claim token does not authorize transition")
    return guard


def _write_guard(path: Path, guard: AbortGuard) -> AbortGuard:
    atomic_write_bytes(
        path,
        canonical_json_bytes(abort_guard_payload(guard)) + b"\n",
        mode=0o600,
    )
    return guard
