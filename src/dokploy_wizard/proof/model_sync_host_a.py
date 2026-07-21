from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.model_sync_artifacts import (
    JsonValue,
    finalize_capture_outputs,
    sha256_bytes,
    validate_protected_manifest_bytes,
)
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import PreparedEnv, restore_proof_env
from dokploy_wizard.proof.model_sync_remote import RemoteProbe
from dokploy_wizard.proof.model_sync_results import build_result
from dokploy_wizard.proof.model_sync_state import (
    AbortGuard,
    AbortGuardError,
    arm_abort_guard,
    claim_abort_guard,
    clear_env_receipt,
    complete_env_receipt,
    process_identity_matches,
    read_abort_guard,
    reclaim_completed_receipt,
    recover_dead_abort_claim,
    transfer_abort_guard_to_plan,
)


@dataclass(frozen=True, slots=True)
class GuardClaim:
    token: str
    pid: int
    start_time_ticks: str


@dataclass(frozen=True, slots=True)
class ProofRecoveryPaths:

    env_file: Path
    backup_path: Path
    guard_path: Path


@dataclass(frozen=True, slots=True)
class ProofRecovery:

    paths: ProofRecoveryPaths
    claim: GuardClaim


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:

    artifact_dir: Path
    output: Path
    source_base_commit: str
    proof_commit: str
    prepared: PreparedEnv
    guard_path: Path
    host_a: RemoteProbe
    host_b: RemoteProbe
    baseline: CapturedBaseline


def claim_plan_guard(*, guard_path: Path, pid: int, start_time_ticks: str) -> GuardClaim:
    token = secrets.token_urlsafe(24)
    claim_abort_guard(
        guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    return GuardClaim(token=token, pid=pid, start_time_ticks=start_time_ticks)


def begin_proof_recovery(
    *, paths: ProofRecoveryPaths, pid: int, start_time_ticks: str
) -> ProofRecovery:
    if paths.guard_path.exists():
        _recover_existing_guard(paths)
    else:
        arm_abort_guard(paths.guard_path)
    claim = claim_plan_guard(
        guard_path=paths.guard_path, pid=pid, start_time_ticks=start_time_ticks
    )
    return ProofRecovery(paths, claim)


def restore_after_interrupt(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    restore_proof_env(prepared=prepared, guard_path=guard_path)
    clear_env_receipt(guard_path, claim_token=claim.token)
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def recover_failed_proof(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    restore_proof_env(prepared=prepared, guard_path=guard_path)
    guard = read_abort_guard(guard_path)
    if guard.claimant_kind == "process":
        clear_env_receipt(guard_path, claim_token=claim.token)
    guard = read_abort_guard(guard_path)
    if guard.claimant_kind == "process":
        transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def complete_resumable_step(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    del prepared
    complete_env_receipt(guard_path, claim_token=claim.token)
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def recover_interrupted_proof(recovery: ProofRecovery) -> None:
    guard = read_abort_guard(recovery.paths.guard_path)
    if guard.claimant_kind == "plan":
        if guard.env_receipt is None or not guard.env_receipt.complete:
            return
        reclaim_completed_receipt(
            recovery.paths.guard_path,
            pid=recovery.claim.pid,
            start_time_ticks=recovery.claim.start_time_ticks,
            claim_token=recovery.claim.token,
        )
        guard = read_abort_guard(recovery.paths.guard_path)
    if guard.claim_token != recovery.claim.token:
        raise AbortGuardError("abort guard claim token does not authorize recovery")
    if guard.env_receipt is not None and guard.env_receipt.complete:
        transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)
        return
    _restore_receipt(recovery.paths, guard)
    if guard.env_receipt is not None:
        clear_env_receipt(recovery.paths.guard_path, claim_token=recovery.claim.token)
    transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)


def _recover_existing_guard(paths: ProofRecoveryPaths) -> None:
    guard = read_abort_guard(paths.guard_path)
    if guard.state != "armed":
        raise AbortGuardError("existing abort guard is not armed")
    if guard.claimant_kind == "plan":
        if guard.env_receipt is not None:
            if guard.env_receipt.complete:
                raise AbortGuardError("proof baseline is already complete")
            raise AbortGuardError("armed plan guard has an unresolved env receipt")
        return
    assert guard.pid is not None
    assert guard.start_time_ticks is not None
    assert guard.claim_token is not None
    if process_identity_matches(guard.pid, guard.start_time_ticks):
        raise AbortGuardError("existing abort guard is claimed by a live process")
    if guard.env_receipt is not None and guard.env_receipt.complete:
        recover_dead_abort_claim(paths.guard_path, process_identity=process_identity_matches)
        return
    _restore_receipt(paths, guard)
    if guard.env_receipt is not None:
        clear_env_receipt(paths.guard_path, claim_token=guard.claim_token)
    if not recover_dead_abort_claim(paths.guard_path, process_identity=process_identity_matches):
        raise AbortGuardError("dead abort guard claim could not be recovered")


def _restore_receipt(paths: ProofRecoveryPaths, guard: AbortGuard) -> None:
    receipt = guard.env_receipt
    if receipt is None:
        if paths.backup_path.exists():
            raise AbortGuardError("external backup exists without an abort guard receipt")
        return
    if receipt.env_path != str(paths.env_file.resolve()) or receipt.backup_path != str(
        paths.backup_path.resolve()
    ):
        raise AbortGuardError("abort guard receipt paths do not match the requested proof paths")
    restore_proof_env(
        prepared=PreparedEnv(
            paths.env_file,
            paths.backup_path,
            receipt.original_sha256,
            receipt.proof_sha256,
            receipt.mode,
        ),
        guard_path=paths.guard_path,
    )


def finalize_baseline_artifacts(inputs: BaselineArtifactInputs) -> None:
    host_a_path = inputs.artifact_dir / "host-a-preflight.json"
    host_b_path = inputs.artifact_dir / "host-b-preflight.json"
    baseline_path = inputs.artifact_dir / "baseline.json"
    manifest_path = inputs.artifact_dir / "protected-artifacts-before.txt"
    host_a_bytes = _json_bytes(inputs.host_a.to_dict())
    host_b_bytes = _json_bytes(inputs.host_b.to_dict())
    baseline_bytes = _json_bytes(inputs.baseline.payload)
    guard_bytes = inputs.guard_path.read_bytes()
    receipt_path = inputs.artifact_dir / "protected-artifacts-before.sha256"
    if manifest_path.is_symlink() or receipt_path.is_symlink():
        raise ValueError("protected manifest contract must use regular files")
    manifest_bytes = manifest_path.read_bytes()
    validate_protected_manifest_bytes(manifest_bytes)
    receipt_bytes = f"{sha256_bytes(manifest_bytes)}  protected-artifacts-before.txt\n".encode()
    if not manifest_path.is_file() or manifest_path.stat().st_mode & 0o777 != 0o600 or not receipt_path.is_file() or receipt_path.stat().st_mode & 0o777 != 0o600 or receipt_path.read_bytes() != receipt_bytes:  # noqa: E501
        raise ValueError("pre-existing protected manifest receipt is invalid")
    result = build_result(
        {
            "schema_version": 1,
            "source_base_commit": inputs.source_base_commit,
            "proof_commit": inputs.proof_commit,
            "coder_image_digest": inputs.baseline.images["coder"],
            "litellm_image_digest": inputs.baseline.images["litellm"],
            "shared_core_image_digests": {
                "pgvector": inputs.baseline.images["pgvector"],
                "redis": inputs.baseline.images["redis"],
                "postfix": inputs.baseline.images["postfix"],
                "litellm": inputs.baseline.images["litellm"],
            },
            "env_original_sha256": inputs.prepared.original_sha256,
            "env_proof_sha256": inputs.prepared.proof_sha256,
            "env_mode": inputs.prepared.mode,
            "external_backup_path": str(inputs.prepared.backup_path),
            "abort_guard_path": str(inputs.guard_path),
            "abort_guard_sha256": sha256_bytes(guard_bytes),
            "host_a_preflight_sha256": sha256_bytes(host_a_bytes),
            "host_b_preflight_sha256": sha256_bytes(host_b_bytes),
            "host_identities_distinct": True,
            "host_architectures_equal": True,
            "baseline_sha256": sha256_bytes(baseline_bytes),
            "protected_artifacts_before_path": str(manifest_path),
            "protected_artifacts_before_sha256": sha256_bytes(manifest_bytes),
            "coder_secret_inventory_sha256": inputs.baseline.coder_secret_inventory_sha256,
            "legacy_workspace_managed_fingerprints_sha256": (
                inputs.baseline.legacy_workspace_managed_fingerprints_sha256
            ),
        }
    )
    finalize_capture_outputs(
        {
            host_a_path: host_a_bytes,
            host_b_path: host_b_bytes,
            baseline_path: baseline_bytes,
            inputs.output: _json_bytes(result),
        }
    )


def _json_bytes(payload: dict[str, JsonValue]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
