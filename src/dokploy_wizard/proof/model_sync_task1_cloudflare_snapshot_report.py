"""Parse the redacted remote cleanup report consumed by local Task 1 finalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence import (
    validate_cleanup_evidence,
    validate_restoration_evidence,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    CloudflareSnapshotError,
    canonical_snapshot_bytes,
    require_snapshot_hash,
)


class CloudflareSnapshotReportError(CloudflareSnapshotError):
    """Raised when remote cleanup output cannot bind redacted snapshot evidence."""


@dataclass(frozen=True, slots=True)
class Task1CloudflareCleanupReport:
    """Complete redacted cleanup evidence transferred through the bounded wrapper."""

    pre_install: CloudflareSnapshotV1
    post_install: CloudflareSnapshotV1
    post_cleanup: CloudflareSnapshotV1
    receipt_bytes: bytes
    final_journal_sha256: str
    post_install_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class Task1CloudflareRestorationReport:
    """Exact terminal cleanup evidence that cannot enter baseline finalization."""

    pre_install: CloudflareSnapshotV1
    post_install: CloudflareSnapshotV1
    post_cleanup: CloudflareSnapshotV1
    receipt_bytes: bytes
    final_journal_sha256: str
    post_install_snapshot_sha256: str
    status: Literal["cleaned", "restored"]


def parse_cleanup_report(content: bytes) -> Task1CloudflareCleanupReport:
    """Require exact complete report fields before local artifact publication."""
    report = parse_restoration_report(content)
    if report.status != "cleaned":
        raise CloudflareSnapshotReportError("Cloudflare cleanup report binding is invalid")
    validate_cleanup_evidence(
        report.pre_install,
        report.post_cleanup,
        report.receipt_bytes,
        report.final_journal_sha256,
    )
    return Task1CloudflareCleanupReport(
        report.pre_install,
        report.post_install,
        report.post_cleanup,
        report.receipt_bytes,
        report.final_journal_sha256,
        report.post_install_snapshot_sha256,
    )


def parse_restoration_report(content: bytes) -> Task1CloudflareRestorationReport:
    """Require complete terminal cleanup proof before local rollback can proceed."""
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CloudflareSnapshotReportError("Cloudflare cleanup report is invalid") from error
    expected = {
        "cleanup_receipt",
        "cleanup_receipt_sha256",
        "context_sha256",
        "final_journal_sha256",
        "operations_sha256",
        "post_cleanup_cloudflare",
        "post_cleanup_snapshot_sha256",
        "post_install_cloudflare",
        "post_install_snapshot_sha256",
        "pre_install_cloudflare",
        "pre_install_snapshot_sha256",
        "status",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CloudflareSnapshotReportError("Cloudflare cleanup report schema is invalid")
    pre = _snapshot(value["pre_install_cloudflare"])
    post = _snapshot(value["post_install_cloudflare"])
    cleanup = _snapshot(value["post_cleanup_cloudflare"])
    receipt = value["cleanup_receipt"]
    if not isinstance(receipt, Mapping):
        raise CloudflareSnapshotReportError("Cloudflare cleanup report receipt is invalid")
    receipt_bytes = canonical_snapshot_bytes(dict(receipt))
    final_journal_sha256 = _hash(value["final_journal_sha256"])
    status = value["status"]
    receipt_status = receipt.get("status")
    if (
        _hash(value["cleanup_receipt_sha256"]) != hashlib.sha256(receipt_bytes).hexdigest()
        or _hash(value["pre_install_snapshot_sha256"]) != pre.snapshot_sha256
        or _hash(value["post_install_snapshot_sha256"]) != post.snapshot_sha256
        or _hash(value["post_cleanup_snapshot_sha256"]) != cleanup.snapshot_sha256
        or value["context_sha256"] != pre.context_sha256
        or value["operations_sha256"] != final_journal_sha256
        or status not in {"cleaned", "restored"}
        or receipt_status != status
    ):
        raise CloudflareSnapshotReportError("Cloudflare cleanup report binding is invalid")
    validate_restoration_evidence(pre, cleanup, receipt_bytes, final_journal_sha256)
    return Task1CloudflareRestorationReport(
        pre,
        post,
        cleanup,
        receipt_bytes,
        final_journal_sha256,
        post.snapshot_sha256,
        status,
    )


def _snapshot(value: JsonValue) -> CloudflareSnapshotV1:
    if not isinstance(value, Mapping):
        raise CloudflareSnapshotReportError("Cloudflare cleanup report snapshot is invalid")
    return CloudflareSnapshotV1.from_bytes(canonical_snapshot_bytes(dict(value)))


def _hash(value: JsonValue) -> str:
    return require_snapshot_hash(value if isinstance(value, str) else "")
