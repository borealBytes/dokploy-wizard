"""Durable, redacted Task 1 inputs for local-only finalization recovery."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_state import AbortGuardError, read_abort_guard
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)

_MAX_BYTES: Final = 96 * 1024 * 1024


class FinalizationBundlePhase(StrEnum):
    """The only durable stages between post-install capture and local finalization."""

    PENDING_CLEANUP = "pending_cleanup"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class Task1FinalizationPlan:
    """Redacted static values that become immutable before remote cleanup starts."""

    source_base_commit: str
    proof_commit: str
    host_identity_mode: proof.HostIdentityMode
    payloads: dict[str, bytes]
    images: dict[str, str]
    coder_secret_inventory_sha256: str
    legacy_workspace_managed_fingerprints_sha256: str
    preexisting_cloudflare_sha256: str


@dataclass(frozen=True, slots=True)
class Task1FinalizationBundle:
    """Canonical recovery data that never contains hostnames, credentials, or API keys."""

    guard_id: str
    context_sha256: str
    uploaded_env_sha256: str
    host_sha256: str
    source_base_commit: str
    proof_commit: str
    host_identity_mode: proof.HostIdentityMode
    phase: FinalizationBundlePhase
    payloads: dict[str, bytes]
    images: dict[str, str]
    coder_secret_inventory_sha256: str
    legacy_workspace_managed_fingerprints_sha256: str
    preexisting_cloudflare_sha256: str
    post_install_cloudflare_sha256: str | None
    snapshot_evidence: Task1CloudflareSnapshotEvidenceV1 | None

    def to_bytes(self) -> bytes:
        """Serialize only a validated canonical recovery bundle."""
        from dokploy_wizard.proof.model_sync_task1_finalization_bundle_schema import (
            serialize_bundle,
        )

        return serialize_bundle(self)


def finalization_bundle_path(guard_path: Path) -> Path:
    """Return the exact state-owned finalization recovery sidecar path."""
    return guard_path.with_name(f"{guard_path.name}.task1-finalization.json")


def persist_pending_finalization_bundle(
    guard_path: Path, host: str, plan: Task1FinalizationPlan
) -> Task1FinalizationBundle:
    """Fsync all finalization inputs before remote cleanup can make a retry necessary."""
    guard = read_abort_guard(guard_path)
    context_sha256, uploaded_env_sha256 = _context_bindings(guard)
    if guard.phase != "proof_active":
        raise AbortGuardError("Task 1 finalization plan requires a proof-active guard")
    bundle = Task1FinalizationBundle(
        guard_id=guard.guard_id,
        context_sha256=context_sha256,
        uploaded_env_sha256=uploaded_env_sha256,
        host_sha256=_sha256(host),
        source_base_commit=plan.source_base_commit,
        proof_commit=plan.proof_commit,
        host_identity_mode=plan.host_identity_mode,
        phase=FinalizationBundlePhase.PENDING_CLEANUP,
        payloads=dict(plan.payloads),
        images=dict(plan.images),
        coder_secret_inventory_sha256=plan.coder_secret_inventory_sha256,
        legacy_workspace_managed_fingerprints_sha256=(
            plan.legacy_workspace_managed_fingerprints_sha256
        ),
        preexisting_cloudflare_sha256=plan.preexisting_cloudflare_sha256,
        post_install_cloudflare_sha256=None,
        snapshot_evidence=None,
    )
    _write_bundle(guard_path, bundle)
    return bundle


def publish_cleanup_complete_bundle(
    guard_path: Path, host: str, report: Task1CloudflareCleanupReport
) -> Task1FinalizationBundle:
    """Atomically bind a validated cleanup report to a previously fsynced plan."""
    from dokploy_wizard.proof.model_sync_task1_finalization_bundle_publication import (
        completed_baseline_payload,
    )

    bundle = require_finalization_bundle(guard_path, host)
    if bundle.phase is not FinalizationBundlePhase.PENDING_CLEANUP:
        raise AbortGuardError("Task 1 finalization bundle is not awaiting cleanup")
    guard = read_abort_guard(guard_path)
    receipt = guard.env_receipt
    if receipt is None or receipt.context_evidence is None:
        raise AbortGuardError("Task 1 finalization bundle lacks context evidence")
    snapshot_evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        report, receipt.context_evidence
    )
    completed = replace(
        bundle,
        phase=FinalizationBundlePhase.READY,
        payloads={
            **bundle.payloads,
            "baseline.json": completed_baseline_payload(bundle.payloads["baseline.json"], report),
        },
        post_install_cloudflare_sha256=report.post_install_snapshot_sha256,
        snapshot_evidence=snapshot_evidence,
    )
    _write_bundle(guard_path, completed)
    return completed


def require_finalization_bundle(
    guard_path: Path, host: str | None = None
) -> Task1FinalizationBundle:
    """Load a canonical bundle bound to the live guard, context, upload, and optional host."""
    from dokploy_wizard.proof.model_sync_task1_finalization_bundle_schema import parse_bundle

    path = finalization_bundle_path(guard_path)
    try:
        content, _mode = proof.read_bounded_regular_bytes(path, _MAX_BYTES, 0o600)
    except (OSError, ValueError) as error:
        raise AbortGuardError("Task 1 finalization bundle is unreadable") from error
    bundle = parse_bundle(content)
    guard = read_abort_guard(guard_path)
    context_sha256, uploaded_env_sha256 = _context_bindings(guard)
    if (
        bundle.guard_id != guard.guard_id
        or bundle.context_sha256 != context_sha256
        or bundle.uploaded_env_sha256 != uploaded_env_sha256
        or (host is not None and bundle.host_sha256 != _sha256(host))
    ):
        raise AbortGuardError("Task 1 finalization bundle does not match this proof invocation")
    return bundle


def load_finalization_bundle(guard_path: Path) -> Task1FinalizationBundle | None:
    """Load an optional bundle for a claimed finalization-intent recovery path."""
    if not os.path.lexists(finalization_bundle_path(guard_path)):
        return None
    return require_finalization_bundle(guard_path)


def clear_finalization_bundle(guard_path: Path, host: str | None = None) -> None:
    """Remove only exact bytes from the recovery sidecar after completed finalization."""
    bundle = require_finalization_bundle(guard_path, host)
    artifacts.unlink_exact_regular_bytes(finalization_bundle_path(guard_path), bundle.to_bytes())


def _write_bundle(guard_path: Path, bundle: Task1FinalizationBundle) -> None:
    artifacts.atomic_write_bytes(
        finalization_bundle_path(guard_path), bundle.to_bytes(), mode=0o600
    )


def _context_bindings(guard: proof.AbortGuard) -> tuple[str, str]:
    receipt = guard.env_receipt
    if receipt is None or receipt.context_evidence is None:
        raise AbortGuardError("Task 1 finalization bundle lacks context evidence")
    evidence = receipt.context_evidence
    return evidence.context_sha256, evidence.uploaded_env_sha256


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
