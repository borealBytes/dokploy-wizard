"""Direct baseline finalization after all required observations are available."""

from __future__ import annotations

from dataclasses import replace

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_env import restore_proof_env
from dokploy_wizard.proof.model_sync_finalization import (
    BaselineArtifactInputs,
    build_baseline_payloads,
    post_install_cloudflare_sha256,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_finalization import (
    require_baseline_snapshot_evidence,
    snapshot_evidence_for_finalization,
)
from dokploy_wizard.proof.model_sync_task1_evidence import (
    restore_task1_proof_source,
    verify_task1_finalized_evidence,
)


def finalize_baseline_artifacts(
    inputs: BaselineArtifactInputs,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
    artifact_payloads: dict[str, bytes] | None = None,
) -> None:
    """Write attested artifacts after the environment has been durably restored."""
    paths = proof.ProofRecoveryPaths(
        inputs.prepared.env_file,
        inputs.prepared.backup_path,
        inputs.guard_path,
        inputs.artifact_dir,
        inputs.output,
        inputs.repository_root,
    )
    proof.assert_no_generated_outputs(paths)
    _restore_finalization_source(inputs, boundary_hook)
    manifest = proof.protected_bytes(paths)
    payloads = build_baseline_payloads(inputs) if artifact_payloads is None else artifact_payloads
    guard = state.read_abort_guard(inputs.guard_path)
    if guard.env_receipt is None:
        raise proof.AbortGuardError("proof-active guard lacks env receipt")
    snapshot_evidence = snapshot_evidence_for_finalization(
        guard.env_receipt, inputs.cloudflare_cleanup_report
    )
    if snapshot_evidence is not None:
        require_baseline_snapshot_evidence(payloads["baseline.json"], snapshot_evidence)
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
        _optional_payload_hash(payloads, "host-b-preflight.json"),
        _optional_payload_hash(payloads, "single-host-lifecycle-baseline.json"),
        artifacts.sha256_bytes(payloads["baseline.json"]),
        artifacts.sha256_bytes(manifest),
        inputs.baseline.coder_secret_inventory_sha256,
        inputs.baseline.legacy_workspace_managed_fingerprints_sha256,
        inputs.host_a.preexisting_cloudflare_sha256,
        post_install_cloudflare_sha256(inputs),
        snapshot_evidence,
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
    proof.verify_attestation(paths, state.read_abort_guard(inputs.guard_path), require_result=True)
    state.complete_abort_guard(inputs.guard_path, claim_token=inputs.claim.token)
    boundary_hook(proof.FinalizationBoundary.COMPLETE_GUARD)


def _restore_finalization_source(
    inputs: BaselineArtifactInputs, boundary_hook: proof.BoundaryHook
) -> None:
    guard = state.read_abort_guard(inputs.guard_path)
    if inputs.task1_proof is not None:
        if (
            guard.env_receipt is None
            or guard.env_receipt.context_evidence != inputs.task1_proof.evidence
        ):
            raise proof.AbortGuardError(
                "proof-active guard does not bind the Task 1 context evidence"
            )
        restored_evidence = restore_task1_proof_source(
            prepared=inputs.task1_proof, backup_path=inputs.prepared.backup_path
        )
        state.record_env_restored(
            inputs.guard_path,
            claim_token=inputs.claim.token,
            receipt=replace(guard.env_receipt, context_evidence=restored_evidence),
        )
        verify_task1_finalized_evidence(
            evidence=restored_evidence,
            source_path=inputs.prepared.env_file,
            backup_path=inputs.prepared.backup_path,
        )
    elif inputs.prepared.restore_before_finalization:
        restore_proof_env(
            prepared=inputs.prepared,
            guard_path=inputs.guard_path,
            boundary_hook=boundary_hook,
        )
        state.record_env_restored(inputs.guard_path, claim_token=inputs.claim.token)


def _optional_payload_hash(payloads: dict[str, bytes], name: str) -> str | None:
    content = payloads.get(name)
    return None if content is None else artifacts.sha256_bytes(content)
