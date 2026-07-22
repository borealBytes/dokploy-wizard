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
        case "claimed" | "env_intent" | "proof_active" | "rollback":
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
) -> None:
    """Converge a nonterminal guard to exact rollback or durable fail-closed evidence."""
    if recovery.claim is None:
        return
    guard = state.read_abort_guard(recovery.paths.guard_path)
    if guard.phase == "complete" or (guard.phase, guard.claimant_kind) == ("ready", "plan"):
        return
    if guard.phase != "rollback":
        state.begin_rollback(
            recovery.paths.guard_path,
            claim_token=recovery.claim.token,
        )
    try:
        current = state.read_abort_guard(recovery.paths.guard_path)
        if current.env_receipt is not None:
            receipt = current.env_receipt
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
    if guard.claimant_kind == "process":
        if (
            guard.pid is not None
            and guard.start_time_ticks is not None
            and state.process_identity_matches(guard.pid, guard.start_time_ticks)
        ):
            raise proof.AbortGuardError("existing abort guard is claimed by a live process")
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
    recover_interrupted_proof(recovery)
