"""External Task 1 proof files and canonical-source restoration evidence."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    sha256,
    task1_env_bytes,
    task1_namespace,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
)
from dokploy_wizard.proof.model_sync_task1_evidence_validation import (
    read_task1_evidence_file as _read_exact_file,
)
from dokploy_wizard.proof.model_sync_task1_evidence_validation import (
    verify_task1_external_evidence,
)
from dokploy_wizard.proof.model_sync_task1_materialization import task1_overlay

if TYPE_CHECKING:
    from dokploy_wizard.proof.model_sync_task1_context import PreparedTask1ProofContext

_FILE_MODE: Final = 0o600
__all__ = (
    "Task1ProofContextError",
    "Task1ProofContextEvidenceV1",
    "recover_task1_receipt_source",
    "restore_task1_proof_source",
    "restore_task1_receipt_source",
    "task1_env_bytes",
    "task1_namespace",
    "task1_overlay",
    "verify_task1_external_evidence",
    "verify_task1_finalized_evidence",
)


def restore_task1_proof_source(
    *, prepared: PreparedTask1ProofContext, backup_path: Path
) -> Task1ProofContextEvidenceV1:
    return restore_task1_receipt_source(
        evidence=prepared.evidence,
        source_path=prepared.source_path,
        backup_path=backup_path,
    )


def restore_task1_receipt_source(
    *,
    evidence: Task1ProofContextEvidenceV1,
    source_path: Path,
    backup_path: Path,
) -> Task1ProofContextEvidenceV1:
    verify_task1_external_evidence(evidence)
    source = _read_bound_source(evidence, source_path)
    backup = _read_exact_file(backup_path, _FILE_MODE)
    if sha256(backup) != evidence.source_env_sha256:
        raise Task1ProofContextError("Task 1 proof external backup drifted")
    artifacts.unlink_exact_regular_bytes(backup_path, backup)
    return _restored_evidence(evidence, source)


def recover_task1_receipt_source(
    *,
    evidence: Task1ProofContextEvidenceV1,
    source_path: Path,
    backup_path: Path,
) -> Task1ProofContextEvidenceV1:
    verify_task1_external_evidence(evidence)
    source = _read_bound_source(evidence, source_path)
    if os.path.lexists(backup_path):
        backup = _read_exact_file(backup_path, _FILE_MODE)
        if sha256(backup) != evidence.source_env_sha256:
            raise Task1ProofContextError("Task 1 proof external backup drifted")
        artifacts.unlink_exact_regular_bytes(backup_path, backup)
    return _restored_evidence(evidence, source)


def _restored_evidence(
    evidence: Task1ProofContextEvidenceV1, source: bytes
) -> Task1ProofContextEvidenceV1:
    return replace(
        evidence,
        observed_restored_source_sha256=sha256(source),
        observed_restored_source_mode=evidence.source_env_mode,
    )


def verify_task1_finalized_evidence(
    *,
    evidence: Task1ProofContextEvidenceV1,
    source_path: Path,
    backup_path: Path,
) -> None:
    verify_task1_external_evidence(evidence)
    if evidence.observed_restored_source_sha256 is None or os.path.lexists(backup_path):
        detail = "Task 1 proof restoration evidence is absent or backup persists"
        raise Task1ProofContextError(detail)
    source = _read_bound_source(evidence, source_path, mode=evidence.observed_restored_source_mode)
    if sha256(source) != evidence.observed_restored_source_sha256:
        raise Task1ProofContextError("Task 1 proof finalized source drifted")


def _read_bound_source(
    evidence: Task1ProofContextEvidenceV1,
    source_path: Path,
    *,
    mode: int | None = None,
) -> bytes:
    resolved = str(source_path.resolve())
    if resolved != evidence.source_path or sha256(resolved.encode()) != evidence.source_path_sha256:
        raise Task1ProofContextError("Task 1 proof source path drifted")
    source = _read_exact_file(source_path, evidence.source_env_mode if mode is None else mode)
    if sha256(source) != evidence.expected_restored_source_sha256:
        raise Task1ProofContextError("Task 1 proof source bytes drifted")
    return source
