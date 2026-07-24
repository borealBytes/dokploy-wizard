"""Durable redacted Cloudflare snapshot artifact and cleanup receipt checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import read_bounded_regular_bytes
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    CloudflareSnapshotError,
    canonical_snapshot_bytes,
    require_snapshot_hash,
)

SNAPSHOT_MAX_BYTES: Final = 256 * 1024


class CloudflareSnapshotEvidenceError(CloudflareSnapshotError):
    """Raised when durable snapshot or cleanup evidence cannot be bound exactly."""


def load_or_capture_snapshot(
    path: Path,
    scope: CloudflareSnapshotScope,
    capture: Callable[[], CloudflareSnapshotV1],
) -> CloudflareSnapshotV1:
    """Use an existing exact preimage or atomically publish the first capture."""
    if path.exists() or path.is_symlink():
        snapshot = _read_snapshot(path)
        _verify_scope(snapshot, scope)
        return snapshot
    snapshot = capture()
    _verify_scope(snapshot, scope)
    try:
        artifacts.write_or_verify_exact_bytes(path, snapshot.to_bytes())
    except artifacts.CaptureSchemaError as error:
        raise CloudflareSnapshotEvidenceError("Cloudflare snapshot artifact is unsafe") from error
    return snapshot


def load_snapshot(path: Path, scope: CloudflareSnapshotScope) -> CloudflareSnapshotV1:
    """Read one exact persisted snapshot without allowing a replacement capture."""
    snapshot = _read_snapshot(path)
    _verify_scope(snapshot, scope)
    return snapshot


def validate_cleanup_evidence(
    pre_install: CloudflareSnapshotV1,
    post_cleanup: CloudflareSnapshotV1,
    receipt_bytes: bytes,
    final_journal_sha256: str,
) -> None:
    """Require exact preimage restoration and a canonical CLEANED receipt."""
    _validate_restoration_evidence(
        pre_install, post_cleanup, receipt_bytes, final_journal_sha256, {"cleaned"}
    )


def validate_restoration_evidence(
    pre_install: CloudflareSnapshotV1,
    post_cleanup: CloudflareSnapshotV1,
    receipt_bytes: bytes,
    final_journal_sha256: str,
) -> None:
    """Require exact preimage restoration for either terminal cleanup receipt."""
    _validate_restoration_evidence(
        pre_install, post_cleanup, receipt_bytes, final_journal_sha256, {"cleaned", "restored"}
    )


def _validate_restoration_evidence(
    pre_install: CloudflareSnapshotV1,
    post_cleanup: CloudflareSnapshotV1,
    receipt_bytes: bytes,
    final_journal_sha256: str,
    allowed_statuses: set[str],
) -> None:
    """Validate the shared exact-restoration bindings for terminal receipts."""
    if pre_install != post_cleanup:
        raise CloudflareSnapshotEvidenceError(
            "Cloudflare cleanup snapshot does not equal pre-install"
        )
    try:
        value = json.loads(receipt_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CloudflareSnapshotEvidenceError("Cloudflare cleanup receipt is invalid") from error
    receipt = _receipt(value)
    if (
        receipt["context_sha256"] != pre_install.context_sha256
        or receipt["operations_sha256"] != require_snapshot_hash(final_journal_sha256)
        or receipt["status"] not in allowed_statuses
        or receipt_bytes != canonical_snapshot_bytes(receipt)
    ):
        raise CloudflareSnapshotEvidenceError(
            "Cloudflare cleanup receipt does not bind final state"
        )


def _read_snapshot(path: Path) -> CloudflareSnapshotV1:
    try:
        content, _mode = read_bounded_regular_bytes(path, SNAPSHOT_MAX_BYTES, 0o600)
        return CloudflareSnapshotV1.from_bytes(content)
    except (OSError, ValueError) as error:
        raise CloudflareSnapshotEvidenceError(
            "Cloudflare snapshot artifact is unreadable"
        ) from error


def _verify_scope(snapshot: CloudflareSnapshotV1, scope: CloudflareSnapshotScope) -> None:
    if (
        snapshot.context_sha256 != scope.context_sha256
        or snapshot.account_id_sha256 != hashlib.sha256(scope.account_id.encode()).hexdigest()
        or snapshot.zone_id_sha256 != hashlib.sha256(scope.zone_id.encode()).hexdigest()
    ):
        raise CloudflareSnapshotEvidenceError("Cloudflare snapshot artifact scope drifted")


def _receipt(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "context_sha256",
        "operations_sha256",
        "status",
    }:
        raise CloudflareSnapshotEvidenceError("Cloudflare cleanup receipt schema is invalid")
    context = value["context_sha256"]
    operations = value["operations_sha256"]
    status = value["status"]
    if (
        not isinstance(context, str)
        or not isinstance(operations, str)
        or status not in {"cleaned", "restored"}
    ):
        raise CloudflareSnapshotEvidenceError("Cloudflare cleanup receipt schema is invalid")
    return {
        "context_sha256": require_snapshot_hash(context),
        "operations_sha256": require_snapshot_hash(operations),
        "status": status,
    }
