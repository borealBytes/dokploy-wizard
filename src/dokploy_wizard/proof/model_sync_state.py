# ruff: noqa: E501
"""Strict GuardV3 journal for crash-safe Task 1 baseline finalization."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.proof import (
    BaselineAttestation,
    EnvReceipt,
    canonical_json_bytes,
    parse_baseline_attestation,
    parse_env_receipt,
    receipt_identity,
    receipt_payload,
)
from dokploy_wizard.proof.model_sync_artifacts import atomic_write_bytes
from dokploy_wizard.proof.model_sync_results import validate_attestation

_SCHEMA_VERSION: Final = 3
_FILE_MODE: Final = 0o600
GuardState = Literal["armed", "disarmed"]
GuardPhase = Literal[
    "ready", "claimed", "env_intent", "proof_active", "finalize_intent", "rollback", "complete"
]
ClaimantKind = Literal["plan", "process"]


class AbortGuardError(RuntimeError):
    """Raised when durable GuardV3 evidence is invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class AbortGuard:
    guard_id: str
    state: GuardState
    phase: GuardPhase
    claimant_kind: ClaimantKind
    pid: int | None
    start_time_ticks: str | None
    claim_token: str | None
    env_receipt: EnvReceipt | None
    attestation: BaselineAttestation | None


def arm_abort_guard(path: Path) -> AbortGuard:
    """Create one fresh plan-owned ready guard without adopting an existing path."""
    if os.path.lexists(path):
        raise AbortGuardError("existing abort guard must be reconciled before re-arming")
    guard = AbortGuard(secrets.token_hex(32), "armed", "ready", "plan", None, None, None, None, None)
    _write_guard(path, guard)
    return guard


def read_abort_guard(path: Path) -> AbortGuard:
    """Read only one exact canonical mode-0600 regular GuardV3 file."""
    raw = _read_guard_bytes(path)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AbortGuardError("abort guard is unreadable") from error
    if not isinstance(decoded, dict):
        raise AbortGuardError("abort guard must be a JSON object")
    if raw != canonical_json_bytes(decoded) + b"\n":
        raise AbortGuardError("abort guard bytes are not canonical")
    expected = {
        "schema_version", "guard_id", "state", "phase", "claimant_kind", "pid",
        "start_time_ticks", "claim_token", "env_receipt", "attestation",
    }
    if set(decoded) != expected or decoded.get("schema_version") != _SCHEMA_VERSION:
        raise AbortGuardError("abort guard schema is invalid")
    try:
        receipt = parse_env_receipt(decoded.get("env_receipt"))
        attestation = parse_baseline_attestation(decoded.get("attestation"))
        if attestation is not None:
            validate_attestation(attestation)
    except ValueError as error:
        raise AbortGuardError("abort guard attestation is invalid") from error
    guard = AbortGuard(
        _require_guard_id(decoded.get("guard_id")),
        _require_state(decoded.get("state")),
        _require_phase(decoded.get("phase")),
        _require_claimant(decoded.get("claimant_kind")),
        decoded.get("pid"),
        decoded.get("start_time_ticks"),
        decoded.get("claim_token"),
        receipt,
        attestation,
    )
    _validate_guard(guard)
    return guard


def claim_abort_guard(path: Path, *, pid: int, start_time_ticks: str, claim_token: str) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "ready" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a ready plan guard may be claimed")
    if not _valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    claimed = AbortGuard(guard.guard_id, "armed", "claimed", "process", pid, start_time_ticks, claim_token, None, None)
    _write_guard(path, claimed)
    return claimed


def claim_rollback_guard(path: Path, *, pid: int, start_time_ticks: str, claim_token: str) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "rollback" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a plan-owned rollback guard may be claimed")
    if not _valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    claimed = AbortGuard(guard.guard_id, "armed", "rollback", "process", pid, start_time_ticks, claim_token, guard.env_receipt, guard.attestation)
    _write_guard(path, claimed)
    return claimed


def reclaim_finalize_intent(path: Path, *, pid: int, start_time_ticks: str, claim_token: str, process_identity: Callable[[int, str], bool]) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "finalize_intent" or guard.claimant_kind != "process" or guard.pid is None or guard.start_time_ticks is None or process_identity(guard.pid, guard.start_time_ticks):
        raise AbortGuardError("finalization guard is not reclaimable")
    if not _valid_process_identity(pid, start_time_ticks, claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    reclaimed = AbortGuard(guard.guard_id, "armed", guard.phase, "process", pid, start_time_ticks, claim_token, guard.env_receipt, guard.attestation)
    _write_guard(path, reclaimed)
    return reclaimed


def record_env_intent(path: Path, *, claim_token: str, receipt: EnvReceipt) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "claimed")
    intended = AbortGuard(guard.guard_id, "armed", "env_intent", "process", guard.pid, guard.start_time_ticks, claim_token, receipt, None)
    _write_guard(path, intended)
    return intended


def record_proof_active(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "env_intent")
    active = AbortGuard(guard.guard_id, "armed", "proof_active", "process", guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, None)
    _write_guard(path, active)
    return active


def record_finalize_intent(path: Path, *, claim_token: str, attestation: BaselineAttestation) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "proof_active")
    if guard.env_receipt is None or guard.guard_id != attestation.guard_id or receipt_identity(guard.env_receipt) != receipt_identity(attestation.env_receipt):
        raise AbortGuardError("attestation does not bind the active guard receipt")
    intent = AbortGuard(guard.guard_id, "armed", "finalize_intent", "process", guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, attestation)
    _write_guard(path, intent)
    return intent


def complete_abort_guard(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "finalize_intent")
    complete = AbortGuard(guard.guard_id, "armed", "complete", "plan", None, None, None, guard.env_receipt, guard.attestation)
    _write_guard(path, complete)
    return complete


def begin_rollback(path: Path, *, claim_token: str) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.claimant_kind != "process" or guard.claim_token != claim_token or guard.phase == "complete":
        raise AbortGuardError("abort guard claim token does not authorize rollback")
    rollback = AbortGuard(guard.guard_id, "armed", "rollback", "process", guard.pid, guard.start_time_ticks, claim_token, guard.env_receipt, guard.attestation)
    _write_guard(path, rollback)
    return rollback


def transfer_abort_guard_to_plan(path: Path, *, claim_token: str) -> AbortGuard:
    guard = _claimed_guard(path, claim_token, "rollback")
    plan = AbortGuard(guard.guard_id, "armed", "rollback", "plan", None, None, None, guard.env_receipt, guard.attestation)
    _write_guard(path, plan)
    return plan


def reset_abort_guard(path: Path) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "rollback" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a plan-owned rollback guard may be reset")
    fresh = AbortGuard(secrets.token_hex(32), "armed", "ready", "plan", None, None, None, None, None)
    _write_guard(path, fresh)
    return fresh


def recover_dead_abort_claim(path: Path, *, process_identity: Callable[[int, str], bool]) -> bool:
    guard = read_abort_guard(path)
    if guard.claimant_kind == "plan":
        return False
    if guard.pid is None or guard.start_time_ticks is None:
        raise AbortGuardError("process abort guard lacks process identity")
    if process_identity(guard.pid, guard.start_time_ticks):
        return False
    recovered = AbortGuard(guard.guard_id, "armed", "rollback", "plan", None, None, None, guard.env_receipt, guard.attestation)
    _write_guard(path, recovered)
    return True


def disarm_abort_guard(path: Path) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != "complete" or guard.claimant_kind != "plan":
        raise AbortGuardError("only a complete plan guard may be disarmed")
    disarmed = AbortGuard(guard.guard_id, "disarmed", "complete", "plan", None, None, None, guard.env_receipt, guard.attestation)
    _write_guard(path, disarmed)
    return disarmed


def process_identity_matches(pid: int, start_time_ticks: str) -> bool:
    try:
        actual = process_start_time_ticks(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except OSError:
        return False
    return actual == start_time_ticks


def process_start_time_ticks(stat_text: str) -> str:
    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise AbortGuardError("proc stat is malformed")
    return fields[19]


def _claimed_guard(path: Path, claim_token: str, phase: GuardPhase) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.phase != phase or guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize transition")
    return guard


def _write_guard(path: Path, guard: AbortGuard) -> None:
    payload = {
        "attestation": None if guard.attestation is None else guard.attestation.to_payload(),
        "claim_token": guard.claim_token,
        "claimant_kind": guard.claimant_kind,
        "env_receipt": receipt_payload(guard.env_receipt),
        "guard_id": guard.guard_id,
        "phase": guard.phase,
        "pid": guard.pid,
        "schema_version": _SCHEMA_VERSION,
        "start_time_ticks": guard.start_time_ticks,
        "state": guard.state,
    }
    atomic_write_bytes(path, canonical_json_bytes(payload) + b"\n", mode=_FILE_MODE)


def _read_guard_bytes(path: Path) -> bytes:
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise AbortGuardError("abort guard is unreadable") from error
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o777 != _FILE_MODE or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise AbortGuardError("abort guard must be a mode-0600 regular file")
        return os.read(descriptor, max(1, after.st_size + 1))
    finally:
        os.close(descriptor)


def _validate_guard(guard: AbortGuard) -> None:
    process = guard.claimant_kind == "process"
    if process != (guard.pid is not None and guard.start_time_ticks is not None and guard.claim_token is not None):
        raise AbortGuardError("abort guard ownership fields are invalid")
    if process and not _valid_process_identity(guard.pid, guard.start_time_ticks, guard.claim_token):
        raise AbortGuardError("abort guard process identity is invalid")
    legal = {
        "ready": ("plan", False, False), "claimed": ("process", False, False),
        "env_intent": ("process", True, False), "proof_active": ("process", True, False),
        "finalize_intent": ("process", True, True), "complete": ("plan", True, True),
    }
    if guard.phase == "rollback":
        if guard.state != "armed":
            raise AbortGuardError("rollback guard state is invalid")
        return
    owner, receipt, attestation = legal[guard.phase]
    if guard.claimant_kind != owner or (guard.env_receipt is not None) != receipt or (guard.attestation is not None) != attestation or (guard.state == "disarmed" and guard.phase != "complete"):
        raise AbortGuardError("abort guard phase combination is invalid")
    if guard.attestation is not None and (guard.attestation.guard_id != guard.guard_id or guard.env_receipt is None or receipt_identity(guard.attestation.env_receipt) != receipt_identity(guard.env_receipt)):
        raise AbortGuardError("abort guard attestation binding is invalid")


def _require_guard_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(item not in "0123456789abcdef" for item in value) or value == "0" * 64:
        raise AbortGuardError("abort guard id is invalid")
    return value


def _require_state(value: object) -> GuardState:
    match value:
        case "armed":
            return "armed"
        case "disarmed":
            return "disarmed"
        case _:
            raise AbortGuardError("abort guard state is invalid")


def _require_phase(value: object) -> GuardPhase:
    match value:
        case "ready" | "claimed" | "env_intent" | "proof_active" | "finalize_intent" | "rollback" | "complete" as phase:
            return phase
        case _:
            raise AbortGuardError("abort guard phase is invalid")


def _require_claimant(value: object) -> ClaimantKind:
    match value:
        case "plan":
            return "plan"
        case "process":
            return "process"
        case _:
            raise AbortGuardError("abort guard claimant is invalid")


def _valid_process_identity(pid: int | None, start_time_ticks: str | None, claim_token: str | None) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and isinstance(start_time_ticks, str) and start_time_ticks.isdecimal() and isinstance(claim_token, str) and len(claim_token) == 32 and all(character.isalnum() or character in "_-" for character in claim_token)
