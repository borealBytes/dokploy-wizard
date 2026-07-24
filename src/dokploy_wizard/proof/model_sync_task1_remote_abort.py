"""Durable, value-free recovery flags for Task 1 remote Cloudflare cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_state import AbortGuardError, read_abort_guard
from dokploy_wizard.proof.model_sync_task1_context_schema import canonical_json_bytes
from dokploy_wizard.proof.model_sync_task1_evidence_schema import Task1ProofContextEvidenceV1

_MAX_BYTES = 16 * 1024


class Task1RemoteProofPhase(StrEnum):
    """Remote-mutation cleanup states that must survive process termination."""

    REMOTE_MUTATION_POSSIBLE = "remote_mutation_possible"
    REMOTE_CLEANUP_COMPLETE = "remote_cleanup_complete"


@dataclass(frozen=True, slots=True)
class Task1RemoteAbortRecord:
    """Hash-bound cleanup state that never serializes a host or credential."""

    guard_id: str
    context_sha256: str
    uploaded_env_sha256: str
    host_sha256: str
    phase: Task1RemoteProofPhase
    finalization_bundle_sha256: str | None

    def to_bytes(self) -> bytes:
        payload: dict[str, str | int] = {
            "context_sha256": self.context_sha256,
            "finalization_bundle_sha256": self.finalization_bundle_sha256 or "",
            "guard_id": self.guard_id,
            "host_sha256": self.host_sha256,
            "phase": str(self.phase),
            "schema_version": 1,
            "uploaded_env_sha256": self.uploaded_env_sha256,
        }
        return canonical_json_bytes(payload) + b"\n"


def remote_abort_path(guard_path: Path) -> Path:
    """Return the one state-owned recovery sidecar for a proof guard."""
    return guard_path.with_name(f"{guard_path.name}.task1-remote-abort.json")


def record_remote_mutation_possible(guard_path: Path, host: str) -> Task1RemoteAbortRecord:
    """Durably arm same-context remote cleanup before the proof wrapper can mutate."""
    guard = read_abort_guard(guard_path)
    evidence = _task1_evidence(guard)
    record = Task1RemoteAbortRecord(
        guard_id=guard.guard_id,
        context_sha256=evidence.context_sha256,
        uploaded_env_sha256=evidence.uploaded_env_sha256,
        host_sha256=_sha256(host),
        phase=Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE,
        finalization_bundle_sha256=None,
    )
    artifacts.atomic_write_bytes(remote_abort_path(guard_path), record.to_bytes(), mode=0o600)
    return record


def record_remote_cleanup_complete(
    guard_path: Path, host: str, finalization_bundle_sha256: str
) -> Task1RemoteAbortRecord:
    """Advance a validated pending cleanup record without weakening its identity binding."""
    record = require_remote_abort_record(guard_path, host)
    if record.phase is not Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE:
        raise AbortGuardError("Task 1 remote cleanup is not pending")
    if re.fullmatch(r"[a-f0-9]{64}", finalization_bundle_sha256) is None:
        raise AbortGuardError("Task 1 finalization bundle hash is invalid")
    completed = replace(
        record,
        phase=Task1RemoteProofPhase.REMOTE_CLEANUP_COMPLETE,
        finalization_bundle_sha256=finalization_bundle_sha256,
    )
    artifacts.atomic_write_bytes(remote_abort_path(guard_path), completed.to_bytes(), mode=0o600)
    return completed


def require_remote_abort_record(guard_path: Path, host: str) -> Task1RemoteAbortRecord:
    """Load and bind a pending remote cleanup record to the live guard and supplied host."""
    path = remote_abort_path(guard_path)
    try:
        content, _mode = proof.read_bounded_regular_bytes(path, _MAX_BYTES, 0o600)
    except (OSError, ValueError) as error:
        raise AbortGuardError("Task 1 remote cleanup record is unreadable") from error
    record = _parse_record(content)
    guard = read_abort_guard(guard_path)
    evidence = _task1_evidence(guard)
    if (
        record.guard_id != guard.guard_id
        or record.context_sha256 != evidence.context_sha256
        or record.uploaded_env_sha256 != evidence.uploaded_env_sha256
        or record.host_sha256 != _sha256(host)
    ):
        raise AbortGuardError("Task 1 remote cleanup record does not match this proof invocation")
    return record


def clear_remote_abort_record(guard_path: Path, host: str) -> None:
    """Remove only the exact cleanup record after its guarded recovery completes."""
    record = require_remote_abort_record(guard_path, host)
    artifacts.unlink_exact_regular_bytes(remote_abort_path(guard_path), record.to_bytes())


def remote_abort_record_exists(guard_path: Path) -> bool:
    """Check for recovery work without reading or accepting arbitrary sidecar bytes."""
    return os.path.lexists(remote_abort_path(guard_path))


def _task1_evidence(guard: proof.AbortGuard) -> Task1ProofContextEvidenceV1:
    receipt = guard.env_receipt
    if receipt is None or receipt.context_evidence is None:
        raise AbortGuardError("Task 1 remote cleanup guard lacks context evidence")
    return receipt.context_evidence


def _parse_record(content: bytes) -> Task1RemoteAbortRecord:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AbortGuardError("Task 1 remote cleanup record is invalid") from error
    expected = {
        "context_sha256",
        "finalization_bundle_sha256",
        "guard_id",
        "host_sha256",
        "phase",
        "schema_version",
        "uploaded_env_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1:
        raise AbortGuardError("Task 1 remote cleanup record is invalid")
    hashes = tuple(
        value[key] for key in expected - {"finalization_bundle_sha256", "phase", "schema_version"}
    )
    if any(
        not isinstance(item, str) or re.fullmatch(r"[a-f0-9]{64}", item) is None for item in hashes
    ):
        raise AbortGuardError("Task 1 remote cleanup record is invalid")
    try:
        phase = Task1RemoteProofPhase(value["phase"])
        raw_bundle_sha256 = value["finalization_bundle_sha256"]
        bundle_sha256 = None if raw_bundle_sha256 == "" else raw_bundle_sha256
        if (phase is Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE) != (bundle_sha256 is None):
            raise ValueError
        if bundle_sha256 is not None and (
            not isinstance(bundle_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", bundle_sha256) is None
        ):
            raise ValueError
        record = Task1RemoteAbortRecord(
            guard_id=value["guard_id"],
            context_sha256=value["context_sha256"],
            uploaded_env_sha256=value["uploaded_env_sha256"],
            host_sha256=value["host_sha256"],
            phase=phase,
            finalization_bundle_sha256=bundle_sha256,
        )
    except ValueError as error:
        raise AbortGuardError("Task 1 remote cleanup record is invalid") from error
    if content != record.to_bytes():
        raise AbortGuardError("Task 1 remote cleanup record is not canonical")
    return record


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
