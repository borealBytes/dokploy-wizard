"""Host A guard lifecycle helpers for the baseline orchestrator."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.model_sync_artifacts import (
    JsonValue,
    finalize_capture_outputs,
    protected_manifest_bytes,
)
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import PreparedEnv, restore_proof_env
from dokploy_wizard.proof.model_sync_remote import RemoteProbe
from dokploy_wizard.proof.model_sync_results import build_result
from dokploy_wizard.proof.model_sync_state import (
    claim_abort_guard,
    read_abort_guard,
    sha256_bytes,
    transfer_abort_guard_to_plan,
)


@dataclass(frozen=True, slots=True)
class GuardClaim:
    token: str
    pid: int
    start_time_ticks: str


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:
    """Complete value-free capture material required before artifact finalization."""

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
    """Claim durable plan ownership for this exact process before a guarded proof step."""
    token = secrets.token_urlsafe(24)
    claim_abort_guard(
        guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    return GuardClaim(token=token, pid=pid, start_time_ticks=start_time_ticks)


def restore_after_interrupt(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    """Restore exact bytes first, then return the guard to durable plan ownership."""
    restore_proof_env(prepared=prepared, guard_path=guard_path)
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def recover_failed_proof(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    """Restore once after a timeout or signal and leave repeated recovery as a no-op."""
    restore_proof_env(prepared=prepared, guard_path=guard_path)
    guard = read_abort_guard(guard_path)
    if guard.claimant_kind == "process":
        transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def complete_resumable_step(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    """Preserve proof bytes and backup while returning successful work to plan ownership."""
    del prepared
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def finalize_baseline_artifacts(inputs: BaselineArtifactInputs) -> None:
    """Write every required output only after all inventories, hashes, and recovery complete."""
    host_a_path = inputs.artifact_dir / "host-a-preflight.json"
    host_b_path = inputs.artifact_dir / "host-b-preflight.json"
    baseline_path = inputs.artifact_dir / "baseline.json"
    manifest_path = inputs.artifact_dir / "protected-artifacts-before.txt"
    host_a_bytes = _json_bytes(inputs.host_a.to_dict())
    host_b_bytes = _json_bytes(inputs.host_b.to_dict())
    baseline_bytes = _json_bytes(inputs.baseline.payload)
    guard_bytes = inputs.guard_path.read_bytes()
    manifest_bytes = protected_manifest_bytes(
        {
            "abort-guard.json": sha256_bytes(guard_bytes),
            "baseline.json": sha256_bytes(baseline_bytes),
            "env-original": inputs.prepared.original_sha256,
            "env-proof": inputs.prepared.proof_sha256,
            "host-a-preflight.json": sha256_bytes(host_a_bytes),
            "host-b-preflight.json": sha256_bytes(host_b_bytes),
        }
    )
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
            manifest_path: manifest_bytes,
            inputs.output: _json_bytes(result),
        }
    )


def _json_bytes(payload: dict[str, JsonValue]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
