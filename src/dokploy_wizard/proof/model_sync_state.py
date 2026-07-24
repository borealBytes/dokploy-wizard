"""Strict GuardV3 journal for crash-safe Task 1 baseline finalization."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
from pathlib import Path

from dokploy_wizard.proof import (
    AbortGuard,
    AbortGuardError,
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
    validate_attestation,
)
from dokploy_wizard.proof.model_sync_artifacts import atomic_write_bytes

__all__ = (
    "AbortGuard",
    "AbortGuardError",
    "begin_rollback",
    "complete_abort_guard",
    "disarm_abort_guard",
    "process_identity_matches",
    "process_start_time_ticks",
    "reclaim_task1_finalization",
    "reclaim_finalize_intent",
    "record_finalize_intent",
    "recover_dead_abort_claim",
    "reset_abort_guard",
    "transfer_abort_guard_to_plan",
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
        guard.guard_id,
        "armed",
        "claimed",
        "process",
        pid,
        start_time_ticks,
        claim_token,
        None,
        None,
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
        guard.guard_id,
        "armed",
        "rollback",
        "process",
        pid,
        start_time_ticks,
        claim_token,
        guard.env_receipt,
        guard.attestation,
    )
    return _write_guard(path, claimed)


def record_env_intent(path: Path, *, claim_token: str, receipt: EnvReceipt) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "claimed")
    intended = AbortGuard(
        guard.guard_id,
        "armed",
        "env_intent",
        "process",
        guard.pid,
        guard.start_time_ticks,
        claim_token,
        receipt,
        None,
    )
    return _write_guard(path, intended)


def record_proof_active(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "env_intent")
    active = AbortGuard(
        guard.guard_id,
        "armed",
        "proof_active",
        "process",
        guard.pid,
        guard.start_time_ticks,
        claim_token,
        guard.env_receipt,
        None,
    )
    return _write_guard(path, active)


def record_env_restored(
    path: Path, *, claim_token: str, receipt: EnvReceipt | None = None
) -> AbortGuard:
    """Persist exact restoration before allowing result finalization."""
    guard = _claimed_guard(path, claim_token, "proof_active")
    if guard.env_receipt is None:
        raise AbortGuardError("proof-active guard lacks env receipt")
    restored_receipt = guard.env_receipt if receipt is None else receipt
    if receipt is not None and (
        receipt_identity(receipt) != receipt_identity(guard.env_receipt)
        or receipt.context_evidence is None
        or receipt.context_evidence.observed_restored_source_sha256 is None
        or replace(
            receipt.context_evidence,
            observed_restored_source_sha256=None,
            observed_restored_source_mode=None,
        )
        != guard.env_receipt.context_evidence
    ):
        raise AbortGuardError("restored context receipt does not bind the active guard")
    restored = AbortGuard(
        guard.guard_id,
        "armed",
        "env_restored",
        "process",
        guard.pid,
        guard.start_time_ticks,
        claim_token,
        restored_receipt,
        None,
    )
    return _write_guard(path, restored)


def _claimed_guard(
    path: Path, claim_token: str, phase: GuardPhase | tuple[GuardPhase, ...]
) -> AbortGuard:
    guard = read_abort_guard(path)
    if (
        guard.phase not in (phase if isinstance(phase, tuple) else (phase,))
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


from dokploy_wizard.proof.model_sync_state_recovery import (  # noqa: E402
    begin_rollback,
    complete_abort_guard,
    disarm_abort_guard,
    reclaim_finalize_intent,
    reclaim_task1_finalization,
    record_finalize_intent,
    recover_dead_abort_claim,
    reset_abort_guard,
    transfer_abort_guard_to_plan,
)
