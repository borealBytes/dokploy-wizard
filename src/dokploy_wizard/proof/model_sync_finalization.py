"""Mode-bound Task 1 baseline artifact finalization."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import model_sync_lifecycle as lifecycle
from dokploy_wizard.proof import model_sync_preflight_evidence as identity
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import PreparedEnv
from dokploy_wizard.proof.model_sync_remote import RemoteProbe


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:
    repository_root: Path
    artifact_dir: Path
    output: Path
    source_base_commit: str
    proof_commit: str
    prepared: PreparedEnv
    guard_path: Path
    claim: proof.GuardClaim
    host_identity_mode: proof.HostIdentityMode
    host_a: RemoteProbe
    host_b: RemoteProbe | None
    baseline: CapturedBaseline


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
    payloads = _baseline_payloads(inputs)
    guard = state.read_abort_guard(inputs.guard_path)
    if guard.env_receipt is None:
        raise proof.AbortGuardError("proof-active guard lacks env receipt")
    host_b_hash = _optional_payload_hash(payloads, "host-b-preflight.json")
    lifecycle_hash = _optional_payload_hash(
        payloads,
        "single-host-lifecycle-baseline.json",
    )
    evidence = proof.BaselineResultEvidence(
        inputs.source_base_commit,
        inputs.proof_commit,
        inputs.host_identity_mode,
        inputs.baseline.images,
        guard.env_receipt,
        inputs.guard_path,
        inputs.artifact_dir,
        "f" * 64,
        artifacts.sha256_bytes(payloads["host-a-preflight.json"]),
        host_b_hash,
        lifecycle_hash,
        artifacts.sha256_bytes(payloads["baseline.json"]),
        artifacts.sha256_bytes(manifest),
        inputs.baseline.coder_secret_inventory_sha256,
        inputs.baseline.legacy_workspace_managed_fingerprints_sha256,
        inputs.host_a.preexisting_cloudflare_sha256,
    )
    attestation = proof.build_baseline_attestation(
        evidence,
        guard_id=guard.guard_id,
        result_path=inputs.output,
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


def _baseline_payloads(inputs: BaselineArtifactInputs) -> dict[str, bytes]:
    host_a = (
        proof.canonical_json_bytes(
            identity.preflight_evidence(
                inputs.host_a,
                mode=inputs.host_identity_mode,
                role="host_a",
            )
        )
        + b"\n"
    )
    payloads = {
        "baseline.json": proof.canonical_json_bytes(
            {
                **inputs.baseline.payload,
                "preexisting_cloudflare": [
                    resource.to_dict()
                    for resource in inputs.host_a.inventory["cloudflare"]
                    if resource.provenance == "preexisting_unowned"
                ],
            }
        )
        + b"\n",
        "host-a-preflight.json": host_a,
    }
    match inputs.host_identity_mode:
        case "distinct":
            if inputs.host_b is None:
                raise proof.AbortGuardError("distinct-host finalization lacks Host B preflight")
            payloads["host-b-preflight.json"] = (
                proof.canonical_json_bytes(
                    identity.preflight_evidence(inputs.host_b, mode="distinct", role="host_b")
                )
                + b"\n"
            )
        case "single_sequential":
            if inputs.host_b is not None:
                raise proof.AbortGuardError("single-host finalization cannot contain Host B")
            receipt = lifecycle.build_baseline_epoch(
                inputs.host_a,
                lifecycle.HostLifecycleEpoch(
                    epoch_id=secrets.token_hex(32),
                    expected_previous_sha256=None,
                ),
            )
            payloads["single-host-lifecycle-baseline.json"] = receipt.to_bytes()
        case unexpected:
            assert_never(unexpected)
    return payloads


def _optional_payload_hash(payloads: dict[str, bytes], name: str) -> str | None:
    content = payloads.get(name)
    return None if content is None else artifacts.sha256_bytes(content)
