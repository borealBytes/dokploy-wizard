"""Durable abort-guard and same-directory atomic-write primitives."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.proof.model_sync_receipt import (
    EnvReceipt,
    env_receipt_payload,
    parse_env_receipt,
)

_SCHEMA_VERSION: Final = 1
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


def sha256_bytes(value: bytes) -> str:
    """Return the stable SHA-256 digest for non-secret artifact bytes."""
    return hashlib.sha256(value).hexdigest()


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = _FILE_MODE) -> None:
    """Atomically replace a protected file from a mode-0600 sibling temporary file."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)
    _fsync_directory(parent)


def atomic_finalize(*, temp: Path, output: Path) -> None:
    """Finalize a prewritten sibling temporary file without crossing directories."""
    if temp.parent.resolve() != output.parent.resolve():
        raise AbortGuardError("atomic finalization requires temp and output to share a parent")
    if not temp.is_file():
        raise AbortGuardError("atomic finalization requires a regular temporary file")
    descriptor = os.open(temp, os.O_RDONLY)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, output)
    _fsync_directory(output.parent)
def arm_abort_guard(path: Path) -> AbortGuard:
    """Durably arm plan ownership before any backup or proof-env write."""
    if path.exists():
        raise AbortGuardError("existing abort guard must be recovered before re-arming")
    guard = AbortGuard("armed", "plan", None, None, None, None)
    _write_guard(path, guard)
    return guard
def read_abort_guard(path: Path) -> AbortGuard:
    """Parse and strictly validate a durable abort guard without changing it."""
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbortGuardError("abort guard is unreadable") from error
    if not isinstance(decoded, dict):
        raise AbortGuardError("abort guard must be a JSON object")
    expected_keys = {
        "claim_token",
        "claimant_kind",
        "pid",
        "schema_version",
        "start_time_ticks",
        "state",
        "env_receipt",
    }
    if set(decoded) != expected_keys or decoded.get("schema_version") != _SCHEMA_VERSION:
        raise AbortGuardError("abort guard schema is invalid")
    state = decoded.get("state")
    claimant = decoded.get("claimant_kind")
    pid = decoded.get("pid")
    start_time = decoded.get("start_time_ticks")
    token = decoded.get("claim_token")
    try:
        receipt = parse_env_receipt(decoded.get("env_receipt"))
    except ValueError as error:
        raise AbortGuardError("abort guard env receipt is invalid") from error
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
    """CAS-transfer armed plan ownership to one exact orchestrator process."""
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
    """CAS-transfer an exact process claim back to durable plan ownership."""
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
    """Return dead process ownership to the plan, preserving live claims unchanged."""
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
    """Check an exact Linux process identity without trusting PID reuse alone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(fields) > 21 and fields[21] == start_time_ticks


def record_env_receipt(path: Path, *, claim_token: str, receipt: EnvReceipt) -> AbortGuard:
    """Attach one exact recovery receipt to its claiming process."""
    guard = read_abort_guard(path)
    if guard.claimant_kind != "process" or guard.claim_token != claim_token:
        raise AbortGuardError("abort guard claim token does not authorize receipt recording")
    if guard.env_receipt is not None and guard.env_receipt != receipt:
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
    if guard.claimant_kind != "plan":
        raise AbortGuardError("only the plan may disarm an abort guard")
    disarmed = AbortGuard("disarmed", "plan", None, None, None, None)
    _write_guard(path, disarmed)
    return disarmed


def _write_guard(path: Path, guard: AbortGuard) -> None:
    payload = {
        "claim_token": guard.claim_token,
        "claimant_kind": guard.claimant_kind,
        "env_receipt": env_receipt_payload(guard.env_receipt),
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


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
