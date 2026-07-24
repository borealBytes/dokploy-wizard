from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.proof.model_sync_task1_context import PreparedTask1ProofContext
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1
from dokploy_wizard.state import RawEnvInput

NamespaceField = Literal["root_domain", "stack_name", "tunnel_name"]


def _source_values() -> dict[str, str]:
    return {
        "ROOT_DOMAIN": "example.test",
        "PACKS": "seaweedfs,coder",
        "AI_DEFAULT_PROVIDER": "openrouter",
        "AI_DEFAULT_MODEL": "example/model",
    }


def _env_bytes(values: dict[str, str]) -> bytes:
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()


def _prepared(tmp_path: Path) -> PreparedTask1ProofContext:
    from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
    from dokploy_wizard.proof.model_sync_task1_materialization import (
        materialize_task1_external_files,
    )

    source_path = tmp_path / ".install-min.env"
    source_bytes = _env_bytes(_source_values())
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values=_source_values(),
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    return prepared


def _context_for_values(
    prepared: PreparedTask1ProofContext, values: dict[str, str]
) -> Task1ProofContextV1:
    return replace(
        prepared.context,
        uploaded_env_sha256=hashlib.sha256(_env_bytes(values)).hexdigest(),
    )


def test_task1_overlay_uses_canonical_true_disable_flag(tmp_path: Path) -> None:
    # Given
    prepared = _prepared(tmp_path)

    # When
    flag = prepared.uploaded_values["DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD"]

    # Then
    assert flag == "true"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ROOT_DOMAIN", "other.test"),
        ("STACK_NAME", "other-stack"),
        ("CLOUDFLARE_TUNNEL_NAME", "other-tunnel"),
        ("DOKPLOY_SUBDOMAIN", "other-dokploy"),
        ("CODER_SUBDOMAIN", "other-coder"),
        ("SEAWEEDFS_SUBDOMAIN", "other-seaweedfs"),
        ("LITELLM_ADMIN_SUBDOMAIN", "other-litellm"),
        ("DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID", "1" * 32),
        ("DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD", "false"),
    ],
)
def test_context_loader_rejects_each_rehashed_projection_mismatch(
    tmp_path: Path, key: str, value: str
) -> None:
    from dokploy_wizard.proof.model_sync_task1_context import (
        Task1ProofContextError,
        load_task1_proof_context,
    )

    # Given
    prepared = _prepared(tmp_path)
    uploaded = {**prepared.uploaded_values, key: value}
    prepared.context_file.write_bytes(_context_for_values(prepared, uploaded).to_bytes())

    # When / Then
    with pytest.raises(Task1ProofContextError):
        load_task1_proof_context(
            prepared.context_file,
            RawEnvInput(format_version=1, values=uploaded),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("root_domain", "Example.test"),
        ("stack_name", "Task1-stack"),
        ("tunnel_name", "task1 tunnel"),
    ],
)
def test_context_schema_rejects_noncanonical_namespace_components(
    tmp_path: Path, field: NamespaceField, value: str
) -> None:
    from dokploy_wizard.proof.model_sync_task1_context_schema import (
        Task1ProofContextError,
        validate_context,
    )

    # Given
    prepared = _prepared(tmp_path)
    context = _replace_namespace_component(prepared.context, field, value)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        validate_context(context)


def _replace_namespace_component(
    context: Task1ProofContextV1, field: NamespaceField, value: str
) -> Task1ProofContextV1:
    match field:
        case "root_domain":
            return replace(context, root_domain=value)
        case "stack_name":
            return replace(context, stack_name=value)
        case "tunnel_name":
            return replace(context, tunnel_name=value)
        case unexpected:
            assert_never(unexpected)


@pytest.mark.parametrize(
    "path_name", ["uploaded_env_file", "context_file", "seed_file", "receipt_file"]
)
def test_context_external_evidence_rejects_file_mode_drift(tmp_path: Path, path_name: str) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        verify_task1_external_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    getattr(prepared, path_name).chmod(0o644)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


def test_context_external_evidence_rejects_unknown_attempt_file(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        verify_task1_external_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    extra = prepared.proof_directory / "unexpected"
    extra.write_text("unexpected", encoding="utf-8")
    extra.chmod(0o600)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(prepared.evidence)


def test_context_external_evidence_rejects_path_escape(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        verify_task1_external_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    escaped = tmp_path / "escaped.env"
    escaped.write_bytes(prepared.uploaded_env_file.read_bytes())
    escaped.chmod(0o600)
    drifted = replace(
        prepared.evidence,
        upload_env_path=str(escaped.resolve()),
        upload_env_path_sha256=hashlib.sha256(str(escaped.resolve()).encode()).hexdigest(),
    )

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_external_evidence(drifted)


def test_context_restoration_rejects_source_drift_and_preserves_backup(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        restore_task1_proof_source,
    )

    # Given
    prepared = _prepared(tmp_path)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(prepared.source_path.read_bytes())
    backup_path.chmod(0o600)
    prepared.source_path.write_text("ROOT_DOMAIN=drift.test\n", encoding="utf-8")
    prepared.source_path.chmod(0o600)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        restore_task1_proof_source(
            prepared=prepared,
            backup_path=backup_path,
        )
    assert backup_path.exists()


def test_context_restoration_binds_exact_source_and_removes_backup(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        restore_task1_proof_source,
        verify_task1_finalized_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    before = prepared.source_path.read_bytes()
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(before)
    backup_path.chmod(0o600)

    # When
    evidence = restore_task1_proof_source(prepared=prepared, backup_path=backup_path)

    # Then
    assert prepared.source_path.read_bytes() == before
    assert not backup_path.exists()
    verify_task1_finalized_evidence(
        evidence=evidence,
        source_path=prepared.source_path,
        backup_path=backup_path,
    )


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_context_restoration_rejects_source_mode_drift(tmp_path: Path, mode: int) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        restore_task1_proof_source,
    )

    # Given
    prepared = _prepared(tmp_path)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(prepared.source_path.read_bytes())
    backup_path.chmod(0o600)
    prepared.source_path.chmod(mode)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        restore_task1_proof_source(prepared=prepared, backup_path=backup_path)
    assert backup_path.exists()


def test_finalized_context_evidence_rejects_persistent_backup(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        verify_task1_finalized_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(prepared.source_path.read_bytes())
    backup_path.chmod(0o600)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_finalized_evidence(
            evidence=prepared.evidence,
            source_path=prepared.source_path,
            backup_path=backup_path,
        )


def test_finalized_context_evidence_rejects_absent_restoration_observation(
    tmp_path: Path,
) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        verify_task1_finalized_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_finalized_evidence(
            evidence=prepared.evidence,
            source_path=prepared.source_path,
            backup_path=tmp_path / "absent-backup.env",
        )


def test_finalized_context_evidence_rejects_external_file_drift(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_evidence import (
        Task1ProofContextError,
        restore_task1_proof_source,
        verify_task1_finalized_evidence,
    )

    # Given
    prepared = _prepared(tmp_path)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(prepared.source_path.read_bytes())
    backup_path.chmod(0o600)
    evidence = restore_task1_proof_source(prepared=prepared, backup_path=backup_path)
    prepared.seed_file.write_text("drifted\n", encoding="utf-8")

    # When / Then
    with pytest.raises(Task1ProofContextError):
        verify_task1_finalized_evidence(
            evidence=evidence,
            source_path=prepared.source_path,
            backup_path=backup_path,
        )
