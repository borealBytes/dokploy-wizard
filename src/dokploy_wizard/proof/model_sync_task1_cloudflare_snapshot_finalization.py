"""Cloudflare cleanup evidence projection used by Task 1 baseline finalization."""

from __future__ import annotations

import json

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
    validate_baseline_snapshot_evidence_payload,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)


def cloudflare_cleanup_payload(
    report: Task1CloudflareCleanupReport,
) -> dict[str, JsonValue]:
    return {
        "cloudflare_cleanup_receipt": require_mapping(
            json.loads(report.receipt_bytes), "Cloudflare cleanup receipt"
        ),
        "cloudflare_final_journal_sha256": report.final_journal_sha256,
        "post_cleanup_cloudflare": require_mapping(
            json.loads(report.post_cleanup.to_bytes()), "post-cleanup Cloudflare snapshot"
        ),
        "pre_install_cloudflare": require_mapping(
            json.loads(report.pre_install.to_bytes()), "pre-install Cloudflare snapshot"
        ),
    }


def snapshot_evidence_for_finalization(
    receipt: proof.EnvReceipt,
    report: Task1CloudflareCleanupReport | None,
) -> Task1CloudflareSnapshotEvidenceV1 | None:
    context_evidence = receipt.context_evidence
    if context_evidence is None:
        return None
    if report is None:
        raise proof.AbortGuardError("context-active finalization lacks Cloudflare cleanup evidence")
    try:
        return Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
            report,
            context_evidence,
        )
    except ValueError as error:
        raise proof.AbortGuardError("Cloudflare cleanup evidence is invalid") from error


def require_baseline_snapshot_evidence(
    content: bytes,
    evidence: Task1CloudflareSnapshotEvidenceV1,
) -> None:
    try:
        validate_baseline_snapshot_evidence_payload(
            require_mapping(json.loads(content), "baseline snapshot evidence"),
            evidence,
        )
    except (
        artifacts.CaptureSchemaError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise proof.AbortGuardError(
            "baseline snapshot evidence does not bind cleanup evidence"
        ) from error
