import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import dokploy_wizard.proof.model_sync_artifacts as artifacts
import dokploy_wizard.proof.model_sync_env as env
import dokploy_wizard.proof.model_sync_results as results
import dokploy_wizard.proof.model_sync_state as state
from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_remote import RemoteProbe

complete_resumable_finalization = results.complete_resumable_finalization


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:
    repository_root: Path
    artifact_dir: Path
    output: Path
    source_base_commit: str
    proof_commit: str
    prepared: env.PreparedEnv
    guard_path: Path
    claim: proof.GuardClaim
    host_a: RemoteProbe
    host_b: RemoteProbe
    baseline: CapturedBaseline


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


def finalize_baseline_artifacts(
    inputs: BaselineArtifactInputs,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
) -> None:
    paths = proof.ProofRecoveryPaths(
        inputs.prepared.env_file,
        inputs.prepared.backup_path,
        inputs.guard_path,
        inputs.artifact_dir,
        inputs.output,
        inputs.repository_root,
    )
    proof.assert_no_generated_outputs(paths)
    manifest = proof.protected_bytes(paths)
    payloads = {
        "baseline.json": proof.canonical_json_bytes(inputs.baseline.payload) + b"\n",
        "host-a-preflight.json": proof.canonical_json_bytes(inputs.host_a.to_dict()) + b"\n",
        "host-b-preflight.json": proof.canonical_json_bytes(inputs.host_b.to_dict()) + b"\n",
    }
    guard = state.read_abort_guard(inputs.guard_path)
    if guard.env_receipt is None:
        raise proof.AbortGuardError("proof-active guard lacks env receipt")
    evidence = proof.BaselineResultEvidence(
        inputs.source_base_commit,
        inputs.proof_commit,
        inputs.baseline.images,
        guard.env_receipt,
        inputs.guard_path,
        inputs.artifact_dir,
        "f" * 64,
        artifacts.sha256_bytes(payloads["host-a-preflight.json"]),
        artifacts.sha256_bytes(payloads["host-b-preflight.json"]),
        artifacts.sha256_bytes(payloads["baseline.json"]),
        artifacts.sha256_bytes(manifest),
        inputs.baseline.coder_secret_inventory_sha256,
        inputs.baseline.legacy_workspace_managed_fingerprints_sha256,
    )
    attestation = proof.build_baseline_attestation(
        evidence, guard_id=guard.guard_id, result_path=inputs.output
    )
    result = proof.result_bytes_from_attestation(attestation)
    proof.require_generated_bounds(payloads, result)
    state.record_finalize_intent(
        inputs.guard_path,
        claim_token=inputs.claim.token,
        attestation=attestation,
    )
    boundary_hook(proof.FinalizationBoundary.FINALIZE_INTENT)
    for name in sorted(payloads):
        artifacts.write_or_verify_exact_bytes(inputs.artifact_dir / name, payloads[name])
        boundary_hook(proof.FINALIZATION_OUTPUT_BOUNDARIES[name])
    artifacts.write_or_verify_exact_bytes(inputs.output, result)
    boundary_hook(proof.FinalizationBoundary.RESULT_PUBLISHED)
    proof.verify_attestation(
        paths,
        state.read_abort_guard(inputs.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(inputs.guard_path, claim_token=inputs.claim.token)
    boundary_hook(proof.FinalizationBoundary.COMPLETE_GUARD)
