from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.proof.model_sync_task1_context import (
    PreparedTask1ProofContext,
    Task1ProofContextError,
    derive_task1_proof_context,
    load_task1_proof_context,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextV1,
    canonical_json_bytes,
    sha256,
    validate_context,
)
from dokploy_wizard.proof.model_sync_task1_evidence import (
    restore_task1_proof_source,
    verify_task1_external_evidence,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
    validate_task1_context_evidence,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    materialize_task1_external_files,
)
from dokploy_wizard.state import RawEnvInput

ProjectionHash = Literal[
    "normalized_env_sha256",
    "overlay_env_sha256",
    "uploaded_env_sha256",
    "namespace_sha256",
]
ExternalFile = Literal["upload", "context", "seed", "receipt"]


def _env_bytes(values: dict[str, str]) -> bytes:
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()


def _prepared(tmp_path: Path) -> PreparedTask1ProofContext:
    values = {
        "ROOT_DOMAIN": "example.test",
        "PACKS": "seaweedfs,coder",
        "AI_DEFAULT_PROVIDER": "openrouter",
        "AI_DEFAULT_MODEL": "example/model",
    }
    source_path = tmp_path / ".install-min.env"
    source_bytes = _env_bytes(values)
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values=values,
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    return prepared


def _replace_projection_hash(
    context: Task1ProofContextV1, field: ProjectionHash
) -> Task1ProofContextV1:
    match field:
        case "normalized_env_sha256":
            return replace(context, normalized_env_sha256="f" * 64)
        case "overlay_env_sha256":
            return replace(context, overlay_env_sha256="f" * 64)
        case "uploaded_env_sha256":
            return replace(context, uploaded_env_sha256="f" * 64)
        case "namespace_sha256":
            return replace(context, namespace_sha256="f" * 64)
        case unexpected:
            assert_never(unexpected)


def _replace_external_path(
    evidence: Task1ProofContextEvidenceV1,
    field: ExternalFile,
    path: Path,
) -> Task1ProofContextEvidenceV1:
    resolved = str(path.resolve())
    digest = sha256(resolved.encode())
    match field:
        case "upload":
            return replace(evidence, upload_env_path=resolved, upload_env_path_sha256=digest)
        case "context":
            return replace(evidence, context_path=resolved, context_path_sha256=digest)
        case "seed":
            return replace(evidence, seed_path=resolved, seed_path_sha256=digest)
        case "receipt":
            return replace(evidence, receipt_path=resolved, receipt_path_sha256=digest)
        case unexpected:
            assert_never(unexpected)


def _external_path(prepared: PreparedTask1ProofContext, field: ExternalFile) -> Path:
    match field:
        case "upload":
            return prepared.uploaded_env_file
        case "context":
            return prepared.context_file
        case "seed":
            return prepared.seed_file
        case "receipt":
            return prepared.receipt_file
        case unexpected:
            assert_never(unexpected)


def test_context_loader_rejects_normalized_upload_drift_when_aggregate_is_rehashed(
    tmp_path: Path,
) -> None:
    # Given
    prepared = _prepared(tmp_path)
    uploaded = {**prepared.uploaded_values, "PACKS": "coder"}
    context = replace(
        prepared.context,
        uploaded_env_sha256=hashlib.sha256(_env_bytes(uploaded)).hexdigest(),
    )
    prepared.context_file.write_bytes(context.to_bytes())

    # When / Then
    with pytest.raises(Task1ProofContextError):
        load_task1_proof_context(
            prepared.context_file,
            RawEnvInput(format_version=1, values=uploaded),
        )


@pytest.mark.parametrize(
    "field",
    [
        "normalized_env_sha256",
        "overlay_env_sha256",
        "uploaded_env_sha256",
        "namespace_sha256",
    ],
)
def test_context_loader_rejects_tampered_projection_hash(
    tmp_path: Path, field: ProjectionHash
) -> None:
    # Given
    prepared = _prepared(tmp_path)
    context = _replace_projection_hash(prepared.context, field)
    prepared.context_file.write_bytes(context.to_bytes())

    # When / Then
    with pytest.raises(Task1ProofContextError):
        load_task1_proof_context(
            prepared.context_file,
            RawEnvInput(format_version=1, values=prepared.uploaded_values),
        )


def test_context_schema_rejects_distinct_source_and_restoration_hashes(tmp_path: Path) -> None:
    # Given
    context = replace(_prepared(tmp_path).context, expected_restored_source_sha256="f" * 64)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        validate_context(context)


def test_context_evidence_rejects_distinct_source_and_restoration_hashes(
    tmp_path: Path,
) -> None:
    # Given
    evidence = replace(
        _prepared(tmp_path).evidence,
        expected_restored_source_sha256="f" * 64,
    )

    # When / Then
    with pytest.raises(Task1ProofContextError):
        validate_task1_context_evidence(evidence)


def test_context_evidence_rejects_proof_directory_hash_mismatch(tmp_path: Path) -> None:
    # Given
    evidence = replace(_prepared(tmp_path).evidence, proof_directory_sha256="f" * 64)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(evidence)


def test_context_evidence_rejects_semantically_drifted_rehashed_receipt(tmp_path: Path) -> None:
    # Given
    prepared = _prepared(tmp_path)
    receipt_payload: dict[str, str | int] = {
        "context_sha256": prepared.evidence.context_sha256,
        "expected_restored_source_sha256": (prepared.evidence.expected_restored_source_sha256),
        "namespace_sha256": "f" * 64,
        "source_env_mode": prepared.evidence.source_env_mode,
        "source_env_sha256": prepared.evidence.source_env_sha256,
        "source_path_sha256": prepared.evidence.source_path_sha256,
        "uploaded_env_sha256": prepared.evidence.uploaded_env_sha256,
    }
    receipt = canonical_json_bytes(receipt_payload)
    prepared.receipt_file.write_bytes(receipt)
    evidence = replace(prepared.evidence, overlay_receipt_sha256=sha256(receipt))

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(evidence)


@pytest.mark.parametrize("field", ["upload", "context", "seed", "receipt"])
def test_context_evidence_rejects_each_external_path_escape(
    tmp_path: Path, field: ExternalFile
) -> None:
    # Given
    prepared = _prepared(tmp_path)
    escaped = tmp_path / f"escaped-{field}"
    escaped.write_bytes(_external_path(prepared, field).read_bytes())
    escaped.chmod(0o600)
    evidence = _replace_external_path(prepared.evidence, field, escaped)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(evidence)


@pytest.mark.parametrize("field", ["upload", "context", "seed", "receipt"])
def test_context_evidence_rejects_each_missing_external_file(
    tmp_path: Path, field: ExternalFile
) -> None:
    # Given
    prepared = _prepared(tmp_path)
    _external_path(prepared, field).unlink()

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


@pytest.mark.parametrize("field", ["upload", "context", "seed", "receipt"])
def test_context_evidence_rejects_each_symlinked_external_file(
    tmp_path: Path, field: ExternalFile
) -> None:
    # Given
    prepared = _prepared(tmp_path)
    path = _external_path(prepared, field)
    target = tmp_path / f"target-{field}"
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


def test_context_evidence_rejects_wrong_mode_proof_directory(tmp_path: Path) -> None:
    # Given
    prepared = _prepared(tmp_path)
    prepared.proof_directory.chmod(0o750)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


def test_context_evidence_rejects_symlinked_proof_directory(tmp_path: Path) -> None:
    # Given
    prepared = _prepared(tmp_path)
    moved = tmp_path / "moved-proof"
    prepared.proof_directory.rename(moved)
    prepared.proof_directory.symlink_to(moved, target_is_directory=True)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


def test_context_restoration_rejects_wrong_mode_backup(tmp_path: Path) -> None:
    # Given
    prepared = _prepared(tmp_path)
    backup = tmp_path / "backup.env"
    backup.write_bytes(prepared.source_path.read_bytes())
    backup.chmod(0o640)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        restore_task1_proof_source(prepared=prepared, backup_path=backup)
