"""Task 1 cleanup report construction with deterministic crash boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from dokploy_wizard.proof import read_bounded_regular_bytes
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_runtime import (
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import JournalStatus
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_delta import validate_install_delta
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence import (
    load_or_capture_snapshot,
    load_snapshot,
    validate_restoration_evidence,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError

if TYPE_CHECKING:
    from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_cleanup import (
        Task1CloudflareCleanupBackend,
    )


class Task1CloudflareCleanupBoundary(StrEnum):
    """Durable points at which tests can simulate a process crash."""

    JOURNAL_CLEANED = "journal-cleaned"
    POST_CLEANUP_SNAPSHOT_PUBLISHED = "post-cleanup-snapshot-published"
    RECEIPT_VALIDATED = "receipt-validated"


Task1CloudflareCleanupHook = Callable[[Task1CloudflareCleanupBoundary], None]


@dataclass(frozen=True, slots=True)
class Task1CloudflareCleanupRun:
    """Dependencies needed to complete and report one Task 1 Cloudflare cleanup."""

    state_dir: Path
    scope: CloudflareSnapshotScope
    journal: Task1CloudflareJournal
    backend: Task1CloudflareCleanupBackend
    capture_snapshot: Callable[[], CloudflareSnapshotV1]
    reconcile_intents: Callable[[], None] | None = None


def ignore_task1_cloudflare_cleanup_boundary(_boundary: Task1CloudflareCleanupBoundary) -> None:
    """Leave production cleanup uninterrupted."""


def run_task1_cloudflare_cleanup(
    run: Task1CloudflareCleanupRun,
    boundary_hook: Task1CloudflareCleanupHook = ignore_task1_cloudflare_cleanup_boundary,
) -> dict[str, JsonValue]:
    """Reuse exact artifacts while completing cleanup and assembling its redacted report."""
    if run.reconcile_intents is not None:
        run.reconcile_intents()
    pre_install = load_snapshot(
        run.state_dir / "task1-cloudflare-pre-install.snapshot.json", run.scope
    )
    post_install = load_or_capture_snapshot(
        run.state_dir / "task1-cloudflare-post-install.snapshot.json",
        run.scope,
        run.capture_snapshot,
    )
    validate_install_delta(pre_install, post_install, run.journal.document())
    receipt = run.journal.cleanup(run.backend)
    final_journal = run.journal.document()
    match final_journal.status:
        case JournalStatus.CLEANED | JournalStatus.RESTORED:
            pass
        case JournalStatus.ACTIVE:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is not terminal")
    final_journal_sha256 = hashlib.sha256(final_journal.to_bytes()).hexdigest()
    boundary_hook(Task1CloudflareCleanupBoundary.JOURNAL_CLEANED)
    post_cleanup = load_or_capture_snapshot(
        run.state_dir / "task1-cloudflare-post-cleanup.snapshot.json",
        run.scope,
        run.capture_snapshot,
    )
    boundary_hook(Task1CloudflareCleanupBoundary.POST_CLEANUP_SNAPSHOT_PUBLISHED)
    receipt_bytes, _mode = read_bounded_regular_bytes(
        run.journal.path.with_suffix(".receipt.json"), 256 * 1024, 0o600
    )
    validate_restoration_evidence(pre_install, post_cleanup, receipt_bytes, final_journal_sha256)
    boundary_hook(Task1CloudflareCleanupBoundary.RECEIPT_VALIDATED)
    return {
        **receipt,
        "cleanup_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "final_journal_sha256": final_journal_sha256,
        "post_cleanup_snapshot_sha256": post_cleanup.snapshot_sha256,
        "post_install_snapshot_sha256": post_install.snapshot_sha256,
        "post_install_cloudflare": json.loads(post_install.to_bytes()),
        "post_cleanup_cloudflare": json.loads(post_cleanup.to_bytes()),
        "pre_install_cloudflare": json.loads(pre_install.to_bytes()),
        "cleanup_receipt": json.loads(receipt_bytes),
        "pre_install_snapshot_sha256": pre_install.snapshot_sha256,
        "status": str(final_journal.status),
    }
