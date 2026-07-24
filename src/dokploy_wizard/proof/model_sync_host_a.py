import os
import secrets
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_env as env
from dokploy_wizard.proof import model_sync_results as results
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_finalization import (
    BaselineArtifactInputs as BaselineArtifactInputs,
)
from dokploy_wizard.proof.model_sync_finalization import (
    finalize_baseline_artifacts as finalize_baseline_artifacts,
)
from dokploy_wizard.proof.model_sync_task1_evidence import recover_task1_receipt_source
from dokploy_wizard.proof.model_sync_task1_partial_cleanup import (
    cleanup_task1_partial_materialization,
)

complete_resumable_finalization = results.complete_resumable_finalization


def claim_plan_guard(*, guard_path: Path, pid: int, start_time_ticks: str) -> proof.GuardClaim:
    token = secrets.token_urlsafe(24)
    state.claim_abort_guard(
        guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    return proof.GuardClaim(token, pid, start_time_ticks)


def begin_proof_recovery(
    *, paths: proof.ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> proof.ProofRecovery:
    """Reconcile disk state before issuing one new process ownership claim."""
    proof.protected_bytes(paths)
    if not os.path.lexists(paths.guard_path):
        proof.assert_no_generated_outputs(paths)
        state.arm_abort_guard(paths.guard_path)
    guard = state.read_abort_guard(paths.guard_path)
    match guard.phase:
        case "complete":
            proof.verify_attestation(paths, guard, require_result=True)
            return proof.ProofRecovery(paths, None, True, False)
        case "ready":
            proof.assert_no_generated_outputs(paths)
        case "finalize_intent":
            if (
                guard.pid is not None
                and guard.start_time_ticks is not None
                and state.process_identity_matches(guard.pid, guard.start_time_ticks)
            ):
                raise proof.AbortGuardError("existing abort guard is claimed by a live process")
            token = secrets.token_urlsafe(24)
            state.reclaim_finalize_intent(
                paths.guard_path,
                pid=pid,
                start_time_ticks=start_time_ticks,
                claim_token=token,
                process_identity=state.process_identity_matches,
            )
            claim = proof.GuardClaim(token, pid, start_time_ticks)
            recovery = proof.ProofRecovery(
                paths,
                claim,
                False,
                proof.attestation_is_resumable(
                    paths,
                    state.read_abort_guard(paths.guard_path),
                ),
            )
            if recovery.resumable:
                return recovery
            recover_interrupted_proof(recovery)
        case "claimed" | "env_intent" | "proof_active" | "env_restored" | "rollback":
            _recover_incomplete_guard(paths, pid, start_time_ticks, guard)
        case unexpected:
            raise proof.AbortGuardError(f"unsupported abort guard phase: {unexpected}")
    claim = claim_plan_guard(
        guard_path=paths.guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
    )
    return proof.ProofRecovery(paths, claim, False, False)


def recover_interrupted_proof(
    recovery: proof.ProofRecovery,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
    task1_partial_cleaned: bool = False,
) -> None:
    """Converge a nonterminal guard to exact rollback or durable fail-closed evidence."""
    if recovery.claim is None:
        return
    guard = state.read_abort_guard(recovery.paths.guard_path)
    if guard.phase == "complete" or (guard.phase, guard.claimant_kind) == ("ready", "plan"):
        return
    receipt = guard.env_receipt
    context_evidence = receipt.context_evidence if receipt is not None else None
    pre_active_context = (
        guard.phase == "env_intent" and receipt is not None and context_evidence is not None
    )
    if pre_active_context and receipt is not None and context_evidence is not None:
        cleanup_task1_partial_materialization(context_evidence, Path(receipt.backup_path))
    if guard.phase != "rollback":
        state.begin_rollback(
            recovery.paths.guard_path,
            claim_token=recovery.claim.token,
        )
    try:
        current = state.read_abort_guard(recovery.paths.guard_path)
        if current.env_receipt is not None:
            receipt = current.env_receipt
            if receipt.context_evidence is None:
                env.restore_proof_env(
                    prepared=env.PreparedEnv(
                        Path(receipt.env_path),
                        Path(receipt.backup_path),
                        receipt.original_sha256,
                        receipt.proof_sha256,
                        receipt.mode,
                    ),
                    guard_path=recovery.paths.guard_path,
                    boundary_hook=boundary_hook,
                )
            else:
                if not (pre_active_context or task1_partial_cleaned):
                    recover_task1_receipt_source(
                        evidence=receipt.context_evidence,
                        source_path=Path(receipt.env_path),
                        backup_path=Path(receipt.backup_path),
                    )
        if current.attestation is None:
            proof.assert_no_generated_outputs(recovery.paths)
        else:
            results.remove_authorized_outputs(
                recovery.paths, current.attestation, boundary_hook=boundary_hook
            )
    except (proof.AbortGuardError, proof.CaptureSchemaError, env.EnvPreparationError, OSError):
        state.transfer_abort_guard_to_plan(
            recovery.paths.guard_path, claim_token=recovery.claim.token
        )
        raise
    state.transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)
    state.reset_abort_guard(recovery.paths.guard_path)


def _recover_incomplete_guard(
    paths: proof.ProofRecoveryPaths,
    pid: int,
    start_time_ticks: str,
    guard: proof.AbortGuard,
) -> None:
    task1_partial_cleaned = False
    if guard.claimant_kind == "process":
        if (
            guard.pid is not None
            and guard.start_time_ticks is not None
            and state.process_identity_matches(guard.pid, guard.start_time_ticks)
        ):
            raise proof.AbortGuardError("existing abort guard is claimed by a live process")
        if (
            guard.phase == "env_intent"
            and guard.env_receipt is not None
            and guard.env_receipt.context_evidence is not None
        ):
            cleanup_task1_partial_materialization(
                guard.env_receipt.context_evidence,
                Path(guard.env_receipt.backup_path),
            )
            task1_partial_cleaned = True
        state.recover_dead_abort_claim(
            paths.guard_path,
            process_identity=state.process_identity_matches,
        )
    rollback = state.read_abort_guard(paths.guard_path)
    if rollback.phase != "rollback" or rollback.claimant_kind != "plan":
        raise proof.AbortGuardError("incomplete guard cannot be reconciled")
    token = secrets.token_urlsafe(24)
    state.claim_rollback_guard(
        paths.guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    recovery = proof.ProofRecovery(
        paths,
        proof.GuardClaim(token, pid, start_time_ticks),
        False,
        False,
    )
    recover_interrupted_proof(recovery, task1_partial_cleaned=task1_partial_cleaned)
