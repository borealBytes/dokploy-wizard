from __future__ import annotations

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

GuardClaim = results.GuardClaim
ProofRecovery = results.ProofRecovery
ProofRecoveryPaths = results.ProofRecoveryPaths
_open_directory = proof.open_protected_directory
_protected_bytes = results.protected_bytes
_verify_protected_artifacts = proof.verify_protected_artifacts


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:
    repository_root: Path
    artifact_dir: Path
    output: Path
    source_base_commit: str
    proof_commit: str
    prepared: env.PreparedEnv
    guard_path: Path
    claim: GuardClaim
    host_a: RemoteProbe
    host_b: RemoteProbe
    baseline: CapturedBaseline


def claim_plan_guard(*, guard_path: Path, pid: int, start_time_ticks: str) -> GuardClaim:
    token = secrets.token_urlsafe(24)
    state.claim_abort_guard(
        guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    return GuardClaim(token, pid, start_time_ticks)


def begin_proof_recovery(
    *, paths: ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> ProofRecovery:
    """Reconcile disk state before issuing one new process ownership claim."""
    _protected_bytes(paths)
    if not os.path.lexists(paths.guard_path):
        results.assert_no_generated_outputs(paths)
        state.arm_abort_guard(paths.guard_path)
    guard = state.read_abort_guard(paths.guard_path)
    match guard.phase:
        case "complete":
            results.verify_attestation(paths, guard, require_result=True)
            return ProofRecovery(paths, None, True, False)
        case "ready":
            results.assert_no_generated_outputs(paths)
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
            claim = GuardClaim(token, pid, start_time_ticks)
            recovery = ProofRecovery(paths, claim, False, _is_resumable(paths))
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
    return ProofRecovery(paths, claim, False, False)


def recover_interrupted_proof(recovery: ProofRecovery) -> None:
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
            )
        if current.attestation is None:
            results.assert_no_generated_outputs(recovery.paths)
        else:
            results.remove_authorized_outputs(recovery.paths, current.attestation)
    except (proof.AbortGuardError, proof.CaptureSchemaError, env.EnvPreparationError, OSError):
        state.transfer_abort_guard_to_plan(
            recovery.paths.guard_path, claim_token=recovery.claim.token
        )
        raise
    state.transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)
    state.reset_abort_guard(recovery.paths.guard_path)


def complete_resumable_finalization(recovery: ProofRecovery) -> None:
    if recovery.claim is None or not recovery.resumable:
        raise proof.AbortGuardError("no resumable finalization is active")
    guard = state.read_abort_guard(recovery.paths.guard_path)
    if guard.attestation is None:
        raise proof.AbortGuardError("finalization intent has no attestation")
    result = results.result_bytes_from_attestation(guard.attestation)
    results.require_generated_bounds({}, result)
    artifacts.write_or_verify_exact_bytes(recovery.paths.output, result)
    results.verify_attestation(
        recovery.paths,
        state.read_abort_guard(recovery.paths.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(recovery.paths.guard_path, claim_token=recovery.claim.token)


def _recover_incomplete_guard(
    paths: ProofRecoveryPaths,
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
    recovery = ProofRecovery(paths, GuardClaim(token, pid, start_time_ticks), False, False)
    recover_interrupted_proof(recovery)


def finalize_baseline_artifacts(inputs: BaselineArtifactInputs) -> None:
    paths = ProofRecoveryPaths(
        inputs.prepared.env_file,
        inputs.prepared.backup_path,
        inputs.guard_path,
        inputs.artifact_dir,
        inputs.output,
        inputs.repository_root,
    )
    results.assert_no_generated_outputs(paths)
    manifest = _protected_bytes(paths)
    payloads = {
        "baseline.json": proof.canonical_json_bytes(inputs.baseline.payload) + b"\n",
        "host-a-preflight.json": proof.canonical_json_bytes(inputs.host_a.to_dict()) + b"\n",
        "host-b-preflight.json": proof.canonical_json_bytes(inputs.host_b.to_dict()) + b"\n",
    }
    guard = state.read_abort_guard(inputs.guard_path)
    if guard.env_receipt is None:
        raise proof.AbortGuardError("proof-active guard lacks env receipt")
    body = results.build_result(
        results.baseline_result_values(inputs, manifest, payloads, "f" * 64)
    )
    output_hashes = {
        **{name: artifacts.sha256_bytes(value) for name, value in payloads.items()},
        "protected-artifacts-before.txt": artifacts.sha256_bytes(manifest),
    }
    result_body = {key: value for key, value in body.items() if key != "abort_guard_sha256"}
    attestation = proof.BaselineAttestation(
        guard.guard_id,
        str(inputs.guard_path.resolve()),
        str(inputs.artifact_dir.resolve()),
        str(inputs.output.resolve()),
        guard.env_receipt,
        output_hashes,
        result_body,
    )
    result = results.result_bytes_from_attestation(attestation)
    results.require_generated_bounds(payloads, result)
    state.record_finalize_intent(
        inputs.guard_path,
        claim_token=inputs.claim.token,
        attestation=attestation,
    )
    for name in sorted(payloads):
        artifacts.write_or_verify_exact_bytes(inputs.artifact_dir / name, payloads[name])
    artifacts.write_or_verify_exact_bytes(inputs.output, result)
    results.verify_attestation(
        paths,
        state.read_abort_guard(inputs.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(inputs.guard_path, claim_token=inputs.claim.token)


def _is_resumable(paths: ProofRecoveryPaths) -> bool:
    try:
        results.verify_attestation(
            paths,
            state.read_abort_guard(paths.guard_path),
            require_result=False,
        )
    except (proof.AbortGuardError, proof.CaptureSchemaError, env.EnvPreparationError, OSError):
        return False
    return True
