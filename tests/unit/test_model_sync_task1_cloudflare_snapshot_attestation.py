from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.proof import (
    AbortGuard,
    BaselineResultEvidence,
    EnvReceipt,
    build_baseline_attestation,
    build_result,
    derive_result_from_attestation,
    parse_baseline_attestation,
    validate_abort_guard,
    validate_attestation,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    active_receipt,
    cleanup_report,
)


def _baseline_evidence(
    tmp_path: Path,
    receipt: EnvReceipt,
    snapshot_evidence: Task1CloudflareSnapshotEvidenceV1 | None = None,
) -> BaselineResultEvidence:
    return BaselineResultEvidence(
        source_base_commit="a" * 40,
        proof_commit="b" * 40,
        host_identity_mode="single_sequential",
        images={
            "coder": "coder@sha256:" + "1" * 64,
            "litellm": "litellm@sha256:" + "2" * 64,
            "pgvector": "pgvector@sha256:" + "3" * 64,
            "redis": "redis@sha256:" + "4" * 64,
            "postfix": "postfix@sha256:" + "5" * 64,
        },
        env_receipt=receipt,
        guard_path=tmp_path / "guard.json",
        artifact_dir=tmp_path,
        abort_guard_sha256="f" * 64,
        host_a_preflight_sha256="a" * 64,
        host_b_preflight_sha256=None,
        single_host_lifecycle_sha256="b" * 64,
        baseline_sha256="c" * 64,
        protected_artifacts_before_sha256="d" * 64,
        coder_secret_inventory_sha256="e" * 64,
        legacy_workspace_managed_fingerprints_sha256="f" * 64,
        preexisting_cloudflare_sha256="1" * 64,
        post_install_cloudflare_sha256=(
            "2" * 64
            if snapshot_evidence is None
            else snapshot_evidence.post_install_snapshot_sha256
        ),
        cloudflare_snapshot_evidence=snapshot_evidence,
    )


def _snapshot_evidence(receipt: EnvReceipt) -> Task1CloudflareSnapshotEvidenceV1:
    assert receipt.context_evidence is not None
    return Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        cleanup_report(receipt.context_evidence.context_sha256),
        receipt.context_evidence,
    )


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, Mapping)
    return dict(value)


def test_context_active_attestation_rejects_absent_snapshot_evidence(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)

    # When / Then
    with pytest.raises(ValueError):
        build_baseline_attestation(
            _baseline_evidence(tmp_path, receipt),
            guard_id="c" * 64,
            result_path=tmp_path / "result.json",
        )


def test_context_active_attestation_serializes_and_parses_snapshot_evidence(
    tmp_path: Path,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    snapshot_evidence = _snapshot_evidence(receipt)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt, snapshot_evidence),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )

    # When
    payload = attestation.to_payload()
    result = derive_result_from_attestation(attestation)

    # Then
    assert payload["schema_version"] == 3
    assert payload["cloudflare_snapshot_evidence"] == snapshot_evidence.to_payload()
    assert result["task1_cloudflare_snapshot_evidence"] == snapshot_evidence.to_payload()
    assert parse_baseline_attestation(payload) == attestation


@pytest.mark.parametrize(
    "field",
    [
        "context_sha256",
        "pre_install_snapshot_sha256",
        "post_install_snapshot_sha256",
        "post_cleanup_snapshot_sha256",
        "otp_provider_sha256",
        "final_journal_sha256",
        "cleanup_receipt_sha256",
    ],
)
def test_context_attestation_rejects_each_rehashed_snapshot_evidence_field(
    tmp_path: Path,
    field: str,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt, _snapshot_evidence(receipt)),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    body = dict(attestation.result_body)
    snapshot_payload = _mapping(body["task1_cloudflare_snapshot_evidence"])
    snapshot_payload[field] = "9" * 64
    body["task1_cloudflare_snapshot_evidence"] = snapshot_payload

    # When / Then
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=body))


def test_context_attestation_parser_rejects_absent_null_or_nonexact_snapshot_block(
    tmp_path: Path,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt, _snapshot_evidence(receipt)),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    # When / Then
    omitted = attestation.to_payload()
    omitted.pop("cloudflare_snapshot_evidence")
    with pytest.raises(ValueError):
        parse_baseline_attestation(omitted)
    null = attestation.to_payload()
    null["cloudflare_snapshot_evidence"] = None
    with pytest.raises(ValueError):
        parse_baseline_attestation(null)
    unknown = attestation.to_payload()
    unknown_block = _mapping(unknown["cloudflare_snapshot_evidence"])
    unknown_block["unexpected"] = "1" * 64
    unknown["cloudflare_snapshot_evidence"] = unknown_block
    with pytest.raises(ValueError):
        parse_baseline_attestation(unknown)
    missing = attestation.to_payload()
    missing_block = _mapping(missing["cloudflare_snapshot_evidence"])
    missing_block.pop("cleanup_receipt_sha256")
    missing["cloudflare_snapshot_evidence"] = missing_block
    with pytest.raises(ValueError):
        parse_baseline_attestation(missing)


def test_result_rejects_snapshot_context_postinstall_and_swapped_snapshot_bindings(
    tmp_path: Path,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt, _snapshot_evidence(receipt)),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    result = derive_result_from_attestation(attestation)

    # When / Then
    context_mismatch = dict(result)
    context_block = _mapping(context_mismatch["task1_cloudflare_snapshot_evidence"])
    context_block["context_sha256"] = "9" * 64
    context_mismatch["task1_cloudflare_snapshot_evidence"] = context_block
    with pytest.raises(ValueError):
        build_result(context_mismatch)
    postinstall_mismatch = dict(result)
    postinstall_block = _mapping(postinstall_mismatch["task1_cloudflare_snapshot_evidence"])
    postinstall_block["post_install_snapshot_sha256"] = "9" * 64
    postinstall_mismatch["task1_cloudflare_snapshot_evidence"] = postinstall_block
    with pytest.raises(ValueError):
        build_result(postinstall_mismatch)
    swapped = dict(attestation.result_body)
    swapped_block = _mapping(swapped["task1_cloudflare_snapshot_evidence"])
    swapped_block["pre_install_snapshot_sha256"], swapped_block["post_install_snapshot_sha256"] = (
        swapped_block["post_install_snapshot_sha256"],
        swapped_block["pre_install_snapshot_sha256"],
    )
    swapped["task1_cloudflare_snapshot_evidence"] = swapped_block
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=swapped))


def test_guard_validation_rejects_snapshot_evidence_replay(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    snapshot_evidence = _snapshot_evidence(receipt)
    attestation = build_baseline_attestation(
        _baseline_evidence(tmp_path, receipt, snapshot_evidence),
        guard_id="c" * 64,
        result_path=tmp_path / "result.json",
    )
    guard = AbortGuard(
        "c" * 64,
        "armed",
        "complete",
        "plan",
        None,
        None,
        None,
        receipt,
        replace(
            attestation,
            cloudflare_snapshot_evidence=replace(
                snapshot_evidence,
                final_journal_sha256="9" * 64,
            ),
        ),
    )

    # When / Then
    with pytest.raises(ValueError):
        validate_abort_guard(guard)
