from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceError,
    Task1CloudflareSnapshotEvidenceV1,
    validate_baseline_snapshot_evidence_payload,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_finalization import (
    require_baseline_snapshot_evidence,
    snapshot_evidence_for_finalization,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    active_receipt,
    cleanup_report,
)

CleanupReportMutation = Callable[[Task1CloudflareCleanupReport], Task1CloudflareCleanupReport]


def _baseline_payload(report: Task1CloudflareCleanupReport) -> dict[str, JsonValue]:
    return {
        "cloudflare_cleanup_receipt": json.loads(report.receipt_bytes),
        "cloudflare_final_journal_sha256": report.final_journal_sha256,
        "post_cleanup_cloudflare": json.loads(report.post_cleanup.to_bytes()),
        "post_install_cloudflare": json.loads(report.post_install.to_bytes()),
        "pre_install_cloudflare": json.loads(report.pre_install.to_bytes()),
    }


def test_snapshot_evidence_v1_serializes_exact_hash_only_payload(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        cleanup_report(receipt.context_evidence.context_sha256),
        receipt.context_evidence,
    )

    # When
    payload = evidence.to_payload()

    # Then
    assert tuple(payload) == (
        "schema_version",
        "context_sha256",
        "pre_install_snapshot_sha256",
        "post_install_snapshot_sha256",
        "post_cleanup_snapshot_sha256",
        "otp_provider_sha256",
        "final_journal_sha256",
        "cleanup_receipt_sha256",
    )
    assert Task1CloudflareSnapshotEvidenceV1.from_payload(payload) == evidence


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("cleanup_receipt_sha256"),
        lambda payload: payload.update({"cleanup_receipt_sha256": None}),
        lambda payload: payload.update({"unknown": "1" * 64}),
        lambda payload: payload.update({"otp_provider_sha256": "0" * 64}),
        lambda payload: payload.update({"final_journal_sha256": "A" * 64}),
    ],
)
def test_snapshot_evidence_v1_rejects_nonexact_payload(
    tmp_path: Path,
    mutate: Callable[[dict[str, JsonValue]], JsonValue],
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        cleanup_report(receipt.context_evidence.context_sha256),
        receipt.context_evidence,
    )
    payload = evidence.to_payload()

    # When / Then
    mutate(payload)
    with pytest.raises(Task1CloudflareSnapshotEvidenceError):
        Task1CloudflareSnapshotEvidenceV1.from_payload(payload)


def test_finalization_derives_evidence_only_from_context_bound_cleanup_report(
    tmp_path: Path,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    report = cleanup_report(receipt.context_evidence.context_sha256)

    # When
    evidence = snapshot_evidence_for_finalization(receipt, report)

    # Then
    assert evidence == Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        report,
        receipt.context_evidence,
    )
    with pytest.raises(proof.AbortGuardError):
        snapshot_evidence_for_finalization(receipt, None)
    with pytest.raises(proof.AbortGuardError):
        snapshot_evidence_for_finalization(
            receipt,
            cleanup_report("9" * 64),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: replace(
            report,
            receipt_bytes=(
                json.dumps(
                    {
                        "context_sha256": report.pre_install.context_sha256,
                        "operations_sha256": "9" * 64,
                        "status": "cleaned",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            ),
        ),
        lambda report: replace(report, final_journal_sha256="9" * 64),
        lambda report: replace(report, pre_install=report.post_install),
        lambda report: replace(
            report,
            post_install=replace(report.post_install, otp_provider_sha256="9" * 64),
        ),
    ],
)
def test_finalization_rejects_receipt_journal_snapshot_or_otp_drift(
    tmp_path: Path,
    mutate: CleanupReportMutation,
) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    report = cleanup_report(receipt.context_evidence.context_sha256)

    # When / Then
    with pytest.raises(proof.AbortGuardError):
        snapshot_evidence_for_finalization(receipt, mutate(report))


def test_baseline_payload_rejects_rehashed_evidence_and_snapshot_replay(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    report = cleanup_report(receipt.context_evidence.context_sha256)
    evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        report,
        receipt.context_evidence,
    )
    payload = _baseline_payload(report)

    # When / Then
    validate_baseline_snapshot_evidence_payload(payload, evidence)
    with pytest.raises(Task1CloudflareSnapshotEvidenceError):
        validate_baseline_snapshot_evidence_payload(
            payload,
            replace(evidence, final_journal_sha256="9" * 64),
        )
    with pytest.raises(Task1CloudflareSnapshotEvidenceError):
        validate_baseline_snapshot_evidence_payload(
            payload,
            replace(evidence, context_sha256="9" * 64),
        )
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    require_baseline_snapshot_evidence(content, evidence)
