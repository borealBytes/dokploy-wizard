"""Durable abort-guard and same-directory atomic-write primitives."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.proof.model_sync_artifacts import atomic_write_bytes
from dokploy_wizard.proof.model_sync_results import (
    EnvReceipt,
    parse_env_receipt,
    receipt_identity,
    receipt_payload,
)

_SCHEMA_VERSION: Final = 2
_FILE_MODE: Final = 0o600


@dataclass(frozen=True, slots=True)
class AbortGuardError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class AbortGuard:
    state: Literal["armed", "disarmed"]
    claimant_kind: Literal["plan", "process"]
    pid: int | None
    start_time_ticks: str | None
    claim_token: str | None
    env_receipt: EnvReceipt | None


def arm_abort_guard(path: Path) -> AbortGuard:
    if path.exists():
        raise AbortGuardError("existing abort guard must be recovered before re-arming")
    guard = AbortGuard("armed", "plan", None, None, None, None)
    _write_guard(path, guard)
    return guard
def read_abort_guard(path: Path) -> AbortGuard:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbortGuardError("abort guard is unreadable") from error
    if not isinstance(decoded, dict):
        raise AbortGuardError("abort guard must be a JSON object")
    expected_v1 = {
        "claim_token",
        "claimant_kind",
        "pid",
        "schema_version",
        "start_time_ticks",
        "state",
    }
    expected_v2 = {*expected_v1, "env_receipt"}
    version = decoded.get("schema_version")
    if version == 1 and set(decoded) == expected_v1:
        receipt = None
    elif version == _SCHEMA_VERSION and set(decoded) == expected_v2:
        try:
            receipt = parse_env_receipt(decoded.get("env_receipt"))
        except ValueError as error:
            raise AbortGuardError("abort guard env receipt is invalid") from error
    else:
        raise AbortGuardError("abort guard schema is invalid")
    state = decoded.get("state")
    claimant = decoded.get("claimant_kind")
    pid = decoded.get("pid")
    start_time = decoded.get("start_time_ticks")
    token = decoded.get("claim_token")
    if state not in {"armed", "disarmed"} or claimant not in {"plan", "process"}:
        raise AbortGuardError("abort guard state is invalid")
    if claimant == "plan" and (pid is not None or start_time is not None or token is not None):
        raise AbortGuardError("plan abort guard must not contain process identity")
    if claimant == "process" and (
        not isinstance(pid, int)
        or not isinstance(start_time, str)
        or not isinstance(token, str)
        or not _valid_claim_token(token)
    ):
        raise AbortGuardError("process abort guard requires complete process identity")
    return AbortGuard(state, claimant, pid, start_time, token, receipt)
def claim_abort_guard(
    path: Path, *, pid: int, start_time_ticks: str, claim_token: str
) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.state != "armed" or guard.claimant_kind != "plan":
        raise AbortGuardError("only an armed plan guard may be claimed")
    if not _valid_claim_token(claim_token):
        raise AbortGuardError("abort guard claim token is invalid")
    claimed = AbortGuard(
        "armed", "process", pid, start_time_ticks, claim_token, guard.env_receipt
    )
    _write_guard(path, claimed)
    return claimed
def transfer_abort_guard_to_plan(path: Path, *, claim_token: str) -> AbortGuard:
    guard = read_abort_guard(path)
    if guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize transfer")
    plan_guard = AbortGuard(guard.state, "plan", None, None, None, guard.env_receipt)
    _write_guard(path, plan_guard)
    return plan_guard
def recover_dead_abort_claim(
    path: Path,
    *,
    process_identity: Callable[[int, str], bool],
) -> bool:
    guard = read_abort_guard(path)
    if guard.claimant_kind == "plan":
        return False
    assert guard.pid is not None
    assert guard.start_time_ticks is not None
    if process_identity(guard.pid, guard.start_time_ticks):
        return False
    _write_guard(path, AbortGuard(guard.state, "plan", None, None, None, guard.env_receipt))
    return True
def process_identity_matches(pid: int, start_time_ticks: str) -> bool:
    try:
        actual = process_start_time_ticks(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except OSError:
        return False
    return actual == start_time_ticks


def process_start_time_ticks(stat: str) -> str:
    """Extract Linux proc-stat field 22 after its parenthesized command field."""
    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise AbortGuardError("proc stat is malformed")
    return fields[19]


def record_env_receipt(path: Path, *, claim_token: str, receipt: EnvReceipt) -> AbortGuard:
    """Attach one exact recovery receipt to its claiming process."""
    guard = read_abort_guard(path)
    if guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize receipt recording")
    if guard.env_receipt is not None and receipt_identity(
        guard.env_receipt
    ) != receipt_identity(receipt):
        raise AbortGuardError("abort guard already has a different env receipt")
    recorded = AbortGuard(
        guard.state,
        guard.claimant_kind,
        guard.pid,
        guard.start_time_ticks,
        guard.claim_token,
        receipt,
    )
    _write_guard(path, recorded)
    return recorded


def complete_env_receipt(path: Path, *, claim_token: str) -> AbortGuard:
    """Mark fully finalized proof outputs as the only plan-owned receipt terminal state."""
    guard = read_abort_guard(path)
    receipt = guard.env_receipt
    if guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize completion")
    if receipt is None:
        return guard
    completed = EnvReceipt(
        receipt.env_path,
        receipt.backup_path,
        receipt.original_sha256,
        receipt.proof_sha256,
        receipt.mode,
        True,
    )
    _write_guard(
        path,
        AbortGuard("armed", "process", guard.pid, guard.start_time_ticks, claim_token, completed),
    )
    return read_abort_guard(path)


def reclaim_completed_receipt(
    path: Path, *, pid: int, start_time_ticks: str, claim_token: str
) -> AbortGuard:
    """Return a completed plan receipt to its original process claim for rollback."""
    guard = read_abort_guard(path)
    if guard.claimant_kind != "plan" or guard.env_receipt is None or not guard.env_receipt.complete:
        raise AbortGuardError("abort guard has no completed receipt to reclaim")
    claimed = claim_abort_guard(
        path, pid=pid, start_time_ticks=start_time_ticks, claim_token=claim_token
    )
    receipt = claimed.env_receipt
    assert receipt is not None
    return record_env_receipt(
        path,
        claim_token=claim_token,
        receipt=EnvReceipt(
            receipt.env_path,
            receipt.backup_path,
            receipt.original_sha256,
            receipt.proof_sha256,
            receipt.mode,
            False,
        ),
    )


def clear_env_receipt(path: Path, *, claim_token: str) -> AbortGuard:
    """Clear a recovered receipt only for its exact process claim."""
    guard = read_abort_guard(path)
    if guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize receipt clearing")
    cleared = AbortGuard(
        guard.state, guard.claimant_kind, guard.pid, guard.start_time_ticks, claim_token, None
    )
    _write_guard(path, cleared)
    return cleared


def disarm_abort_guard(path: Path) -> AbortGuard:
    """Durably mark an already restored proof environment as no longer abortable."""
    guard = read_abort_guard(path)
    if guard.claimant_kind != "plan" or guard.env_receipt is not None:
        raise AbortGuardError("only the plan may disarm an abort guard")
    disarmed = AbortGuard("disarmed", "plan", None, None, None, None)
    _write_guard(path, disarmed)
    return disarmed


def _write_guard(path: Path, guard: AbortGuard) -> None:
    payload = {
        "claim_token": guard.claim_token,
        "claimant_kind": guard.claimant_kind,
        "env_receipt": receipt_payload(guard.env_receipt),
        "pid": guard.pid,
        "schema_version": _SCHEMA_VERSION,
        "start_time_ticks": guard.start_time_ticks,
        "state": guard.state,
    }
    atomic_write_bytes(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _valid_claim_token(value: str) -> bool:
    return len(value) == 32 and all(character.isalnum() or character in "_-" for character in value)
