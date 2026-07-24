"""Local-only Task 1 finalization after durable remote cleanup evidence exists."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import model_sync_results as results
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    FinalizationBundlePhase,
    Task1FinalizationBundle,
    Task1FinalizationPlan,
    clear_finalization_bundle,
    finalization_bundle_path,
    persist_pending_finalization_bundle,
    publish_cleanup_complete_bundle,
    require_finalization_bundle,
)
from dokploy_wizard.proof.model_sync_task1_finalization_evidence import (
    finalization_evidence,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery_helpers import (
    reclaim_task1_recovery,
    restore_bound_source,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    Task1RemoteProofPhase,
    clear_remote_abort_record,
    record_remote_cleanup_complete,
    require_remote_abort_record,
)


class Task1FinalizationRecoveryBoundary(StrEnum):
    """Deterministic crash points around durable Task 1 finalization recovery."""

    PAYLOADS_BUILT = "payloads-built"
    PENDING_BUNDLE_PUBLISHED = "pending-bundle-published"
    CLEANUP_VALIDATED = "cleanup-validated"
    READY_BUNDLE_PUBLISHED = "ready-bundle-published"
    BEFORE_FINALIZE_INTENT = "before-finalize-intent"
    AFTER_FINALIZE_INTENT = "after-finalize-intent"


Task1FinalizationRecoveryHook = Callable[
    [Task1FinalizationRecoveryBoundary | proof.FinalizationBoundary], None
]


def ignore_task1_finalization_recovery_boundary(
    _boundary: Task1FinalizationRecoveryBoundary | proof.FinalizationBoundary,
) -> None:
    """Leave production finalization recovery uninterrupted."""


def persist_task1_finalization_plan(
    guard_path: Path,
    host: str,
    plan: Task1FinalizationPlan,
    boundary_hook: Task1FinalizationRecoveryHook = ignore_task1_finalization_recovery_boundary,
) -> Task1FinalizationBundle:
    """Persist a plan before cleanup can create a local-only recovery obligation."""
    boundary_hook(Task1FinalizationRecoveryBoundary.PAYLOADS_BUILT)
    bundle = persist_pending_finalization_bundle(guard_path, host, plan)
    boundary_hook(Task1FinalizationRecoveryBoundary.PENDING_BUNDLE_PUBLISHED)
    return bundle


def bind_validated_remote_cleanup(
    guard_path: Path,
    host: str,
    report: Task1CloudflareCleanupReport | None,
    boundary_hook: Task1FinalizationRecoveryHook = ignore_task1_finalization_recovery_boundary,
) -> Task1FinalizationBundle:
    """Publish recovery data before recording that remote cleanup has completed."""
    bundle = require_finalization_bundle(guard_path, host)
    match bundle.phase:
        case FinalizationBundlePhase.PENDING_CLEANUP:
            if report is None:
                raise proof.AbortGuardError(
                    "Task 1 cleanup report is required for a pending bundle"
                )
            boundary_hook(Task1FinalizationRecoveryBoundary.CLEANUP_VALIDATED)
            bundle = publish_cleanup_complete_bundle(guard_path, host, report)
            boundary_hook(Task1FinalizationRecoveryBoundary.READY_BUNDLE_PUBLISHED)
        case FinalizationBundlePhase.READY:
            pass
    record_remote_cleanup_complete(
        guard_path,
        host,
        hashlib.sha256(bundle.to_bytes()).hexdigest(),
    )
    return bundle


def complete_task1_finalization(
    recovery: proof.ProofRecovery,
    *,
    boundary_hook: Task1FinalizationRecoveryHook = ignore_task1_finalization_recovery_boundary,
) -> None:
    """Finalize exactly from durable bundle bytes without any remote interaction."""
    if recovery.claim is None:
        raise proof.AbortGuardError("Task 1 finalization recovery has no process claim")
    bundle = require_finalization_bundle(recovery.paths.guard_path)
    if bundle.phase is not FinalizationBundlePhase.READY:
        raise proof.AbortGuardError("Task 1 finalization recovery bundle is not ready")
    receipt = restore_bound_source(recovery)
    proof.assert_no_generated_outputs(recovery.paths)
    manifest = proof.protected_bytes(recovery.paths)
    evidence = finalization_evidence(bundle, receipt, recovery.paths, manifest)
    guard = state.read_abort_guard(recovery.paths.guard_path)
    attestation = proof.build_baseline_attestation(
        evidence, guard_id=guard.guard_id, result_path=recovery.paths.output
    )
    result = proof.result_bytes_from_attestation(attestation)
    proof.require_generated_bounds(bundle.payloads, result)
    boundary_hook(Task1FinalizationRecoveryBoundary.BEFORE_FINALIZE_INTENT)
    state.record_finalize_intent(
        recovery.paths.guard_path,
        claim_token=recovery.claim.token,
        attestation=attestation,
    )
    boundary_hook(Task1FinalizationRecoveryBoundary.AFTER_FINALIZE_INTENT)
    for name in sorted(bundle.payloads):
        artifacts.write_or_verify_exact_bytes(
            recovery.paths.artifact_dir / name, bundle.payloads[name]
        )
        boundary_hook(proof.FINALIZATION_OUTPUT_BOUNDARIES[name])
    artifacts.write_or_verify_exact_bytes(recovery.paths.output, result)
    boundary_hook(proof.FinalizationBoundary.RESULT_PUBLISHED)
    proof.verify_attestation(
        recovery.paths,
        state.read_abort_guard(recovery.paths.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(recovery.paths.guard_path, claim_token=recovery.claim.token)
    boundary_hook(proof.FinalizationBoundary.COMPLETE_GUARD)


def resume_remote_cleanup_finalization(
    paths: proof.ProofRecoveryPaths,
    host: str,
    pid: int,
    start_time_ticks: str,
    *,
    boundary_hook: Task1FinalizationRecoveryHook = ignore_task1_finalization_recovery_boundary,
) -> bool:
    """Resume only a completed remote cleanup, before ordinary guard recovery can reset it."""
    record = require_remote_abort_record(paths.guard_path, host)
    guard = state.read_abort_guard(paths.guard_path)
    if guard.phase == "complete":
        proof.verify_attestation(paths, guard, require_result=True)
        _clear_completed_sidecars(paths.guard_path, host)
        return True
    bundle = require_finalization_bundle(paths.guard_path, host)
    if (
        record.phase is not Task1RemoteProofPhase.REMOTE_CLEANUP_COMPLETE
        or bundle.phase is not FinalizationBundlePhase.READY
        or record.guard_id != bundle.guard_id
        or record.context_sha256 != bundle.context_sha256
        or record.uploaded_env_sha256 != bundle.uploaded_env_sha256
        or record.host_sha256 != bundle.host_sha256
        or record.finalization_bundle_sha256 != hashlib.sha256(bundle.to_bytes()).hexdigest()
    ):
        return False
    recovery = reclaim_task1_recovery(paths, pid, start_time_ticks)
    match guard.phase:
        case "finalize_intent":
            results.complete_resumable_finalization(recovery)
        case "proof_active" | "env_restored":
            complete_task1_finalization(recovery, boundary_hook=boundary_hook)
        case "ready" | "claimed" | "env_intent" | "rollback":
            raise proof.AbortGuardError("completed remote cleanup has an incompatible local guard")
        case unexpected:
            assert_never(unexpected)
    _clear_completed_sidecars(paths.guard_path, host)
    return True


def abort_validated_remote_restoration(
    recovery: proof.ProofRecovery, host: str, *, clear_bundle: bool
) -> None:
    """Reset a locally interrupted proof only after exact remote restoration validates."""
    record = require_remote_abort_record(recovery.paths.guard_path, host)
    match record.phase:
        case Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE:
            pass
        case Task1RemoteProofPhase.REMOTE_CLEANUP_COMPLETE:
            raise proof.AbortGuardError("Task 1 remote cleanup is already finalization-bound")
    restore_bound_source(recovery)
    if clear_bundle:
        clear_finalization_bundle(recovery.paths.guard_path, host)
    clear_remote_abort_record(recovery.paths.guard_path, host)
    from dokploy_wizard.proof.model_sync_host_a import recover_interrupted_proof

    recover_interrupted_proof(recovery)


def _clear_completed_sidecars(guard_path: Path, host: str) -> None:
    if os.path.lexists(finalization_bundle_path(guard_path)):
        clear_finalization_bundle(guard_path, host)
    clear_remote_abort_record(guard_path, host)
