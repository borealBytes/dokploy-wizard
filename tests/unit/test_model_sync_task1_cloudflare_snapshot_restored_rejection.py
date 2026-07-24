from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_finalization import (
    snapshot_evidence_for_finalization,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    CloudflareSnapshotReportError,
    parse_cleanup_report,
    parse_restoration_report,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    active_receipt,
    cleanup_report,
)


def _restored_receipt(context_sha256: str) -> bytes:
    return (
        json.dumps(
            {
                "context_sha256": context_sha256,
                "operations_sha256": "d" * 64,
                "status": "restored",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _restored_report_bytes(context_sha256: str) -> bytes:
    report = cleanup_report(context_sha256)
    receipt_bytes = _restored_receipt(context_sha256)
    value = {
        "cleanup_receipt": json.loads(receipt_bytes),
        "cleanup_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "context_sha256": context_sha256,
        "final_journal_sha256": "d" * 64,
        "operations_sha256": "d" * 64,
        "post_cleanup_cloudflare": json.loads(report.post_cleanup.to_bytes()),
        "post_cleanup_snapshot_sha256": report.post_cleanup.snapshot_sha256,
        "post_install_cloudflare": json.loads(report.post_install.to_bytes()),
        "post_install_snapshot_sha256": report.post_install.snapshot_sha256,
        "pre_install_cloudflare": json.loads(report.pre_install.to_bytes()),
        "pre_install_snapshot_sha256": report.pre_install.snapshot_sha256,
        "status": "restored",
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_restored_cleanup_cannot_parse_or_finalize_as_successful_evidence(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    context_sha256 = receipt.context_evidence.context_sha256
    restored_report = replace(
        cleanup_report(context_sha256), receipt_bytes=_restored_receipt(context_sha256)
    )

    # When / Then
    with pytest.raises(CloudflareSnapshotReportError):
        parse_cleanup_report(_restored_report_bytes(context_sha256))
    with pytest.raises(proof.AbortGuardError):
        snapshot_evidence_for_finalization(receipt, restored_report)


def test_restored_cleanup_parses_only_as_exact_restoration_evidence(tmp_path: Path) -> None:
    # Given
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    content = _restored_report_bytes(receipt.context_evidence.context_sha256)

    # When
    restored = parse_restoration_report(content)

    # Then
    assert restored.pre_install == restored.post_cleanup
    assert restored.status == "restored"
