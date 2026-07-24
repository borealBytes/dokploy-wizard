from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.proof import BaselineResultEvidence, EnvReceipt
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    cleanup_report,
)


def _active_receipt(tmp_path: Path) -> EnvReceipt:
    from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
    from dokploy_wizard.proof.model_sync_task1_evidence import restore_task1_proof_source
    from dokploy_wizard.proof.model_sync_task1_materialization import (
        materialize_task1_external_files,
    )

    source_path = tmp_path / ".install-min.env"
    source_bytes = (
        b"AI_DEFAULT_MODEL=example/model\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"PACKS=coder\nROOT_DOMAIN=example.test\n"
    )
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values={
            "AI_DEFAULT_MODEL": "example/model",
            "AI_DEFAULT_PROVIDER": "openrouter",
            "PACKS": "coder",
            "ROOT_DOMAIN": "example.test",
        },
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(source_bytes)
    backup_path.chmod(0o600)
    evidence = restore_task1_proof_source(prepared=prepared, backup_path=backup_path)
    return EnvReceipt(
        str(source_path.resolve()),
        str(backup_path.resolve()),
        prepared.context.source_env_sha256,
        prepared.context.source_env_sha256,
        0o600,
        evidence,
    )


def _baseline_evidence(tmp_path: Path, receipt: EnvReceipt) -> BaselineResultEvidence:
    assert receipt.context_evidence is not None
    snapshot_evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        cleanup_report(receipt.context_evidence.context_sha256),
        receipt.context_evidence,
    )
    return BaselineResultEvidence(
        "a" * 40,
        "b" * 40,
        "single_sequential",
        {
            "coder": "coder@sha256:" + "1" * 64,
            "litellm": "litellm@sha256:" + "2" * 64,
            "pgvector": "pgvector@sha256:" + "3" * 64,
            "redis": "redis@sha256:" + "4" * 64,
            "postfix": "postfix@sha256:" + "5" * 64,
        },
        receipt,
        tmp_path / "guard.json",
        tmp_path,
        "f" * 64,
        "a" * 64,
        None,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        "1" * 64,
        snapshot_evidence.post_install_snapshot_sha256,
        snapshot_evidence,
    )


def test_context_receipt_uses_versioned_evidence_and_legacy_receipt_stays_exact(
    tmp_path: Path,
) -> None:
    from dokploy_wizard.proof import parse_env_receipt

    # Given
    active = _active_receipt(tmp_path)
    legacy = EnvReceipt("/tmp/env", "/tmp/backup", "a" * 64, "a" * 64, 0o600)

    # When
    active_payload = active.to_payload()
    legacy_payload = legacy.to_payload()

    # Then
    assert active.context_evidence is not None
    assert active_payload["schema_version"] == 2
    assert active_payload["context_evidence"] == active.context_evidence.to_payload()
    assert parse_env_receipt(active_payload) == active
    assert legacy_payload == {
        "backup_path": "/tmp/backup",
        "env_path": "/tmp/env",
        "mode": 0o600,
        "original_sha256": "a" * 64,
        "proof_sha256": "a" * 64,
        "schema_version": 1,
    }


def test_attestation_rejects_result_context_evidence_hash_mismatch(tmp_path: Path) -> None:
    from dokploy_wizard.proof import (
        build_baseline_attestation,
        derive_result_from_attestation,
        validate_attestation,
    )

    # Given
    receipt = _active_receipt(tmp_path)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    body = dict(attestation.result_body)
    assert receipt.context_evidence is not None
    mismatched = dict(receipt.context_evidence.to_payload())
    mismatched["context_sha256"] = "d" * 64
    body["task1_context_evidence"] = mismatched

    # When / Then
    assert derive_result_from_attestation(attestation)["schema_version"] == 3
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=body))


def test_context_attestation_rejects_outer_evidence_hash_mismatch(tmp_path: Path) -> None:
    from dokploy_wizard.proof import build_baseline_attestation, validate_attestation

    # Given
    receipt = _active_receipt(tmp_path)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    assert attestation.context_evidence is not None
    drifted = replace(attestation.context_evidence, context_sha256="d" * 64)

    # When / Then
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, context_evidence=drifted))


def test_context_guard_rejects_restoration_evidence_mismatch(tmp_path: Path) -> None:
    from dokploy_wizard.proof import AbortGuardError
    from dokploy_wizard.proof.model_sync_state import (
        arm_abort_guard,
        claim_abort_guard,
        record_env_intent,
        record_env_restored,
        record_proof_active,
    )

    # Given
    restored = _active_receipt(tmp_path)
    assert restored.context_evidence is not None
    initial_evidence = replace(
        restored.context_evidence,
        observed_restored_source_sha256=None,
        observed_restored_source_mode=None,
    )
    initial = replace(restored, context_evidence=initial_evidence)
    mismatched = replace(
        restored,
        context_evidence=replace(restored.context_evidence, context_sha256="d" * 64),
    )
    guard_path = tmp_path / "guard-state.json"
    arm_abort_guard(guard_path)
    claim_abort_guard(guard_path, pid=123, start_time_ticks="456", claim_token="t" * 32)
    record_env_intent(guard_path, claim_token="t" * 32, receipt=initial)
    record_proof_active(guard_path, claim_token="t" * 32)

    # When / Then
    with pytest.raises(AbortGuardError):
        record_env_restored(
            guard_path,
            claim_token="t" * 32,
            receipt=mismatched,
        )


def test_context_guard_rejects_finalize_intent_before_restoration(tmp_path: Path) -> None:
    from dokploy_wizard.proof import AbortGuardError, build_baseline_attestation
    from dokploy_wizard.proof.model_sync_state import (
        arm_abort_guard,
        claim_abort_guard,
        record_env_intent,
        record_finalize_intent,
        record_proof_active,
    )

    # Given
    restored = _active_receipt(tmp_path)
    assert restored.context_evidence is not None
    initial = replace(
        restored,
        context_evidence=replace(
            restored.context_evidence,
            observed_restored_source_sha256=None,
            observed_restored_source_mode=None,
        ),
    )
    guard_path = tmp_path / "guard-finalize.json"
    guard = arm_abort_guard(guard_path)
    claim_abort_guard(guard_path, pid=123, start_time_ticks="456", claim_token="t" * 32)
    record_env_intent(guard_path, claim_token="t" * 32, receipt=initial)
    record_proof_active(guard_path, claim_token="t" * 32)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, initial),
        guard_id=guard.guard_id,
        result_path=tmp_path / "result.json",
    )

    # When / Then
    with pytest.raises(AbortGuardError):
        record_finalize_intent(
            guard_path,
            claim_token="t" * 32,
            attestation=attestation,
        )
