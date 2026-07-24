"""Attestation evidence reconstructed exclusively from a ready recovery bundle."""

from __future__ import annotations

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    Task1FinalizationBundle,
)


def finalization_evidence(
    bundle: Task1FinalizationBundle,
    receipt: proof.EnvReceipt,
    paths: proof.ProofRecoveryPaths,
    protected_manifest: bytes,
) -> proof.BaselineResultEvidence:
    """Create exact result evidence using no captured runtime object or remote call."""
    if bundle.snapshot_evidence is None or bundle.post_install_cloudflare_sha256 is None:
        raise proof.AbortGuardError("Task 1 finalization bundle lacks cleanup evidence")
    host_b = bundle.payloads.get("host-b-preflight.json")
    lifecycle = bundle.payloads.get("single-host-lifecycle-baseline.json")
    return proof.BaselineResultEvidence(
        bundle.source_base_commit,
        bundle.proof_commit,
        bundle.host_identity_mode,
        bundle.images,
        receipt,
        paths.guard_path,
        paths.artifact_dir,
        "f" * 64,
        artifacts.sha256_bytes(bundle.payloads["host-a-preflight.json"]),
        None if host_b is None else artifacts.sha256_bytes(host_b),
        None if lifecycle is None else artifacts.sha256_bytes(lifecycle),
        artifacts.sha256_bytes(bundle.payloads["baseline.json"]),
        artifacts.sha256_bytes(protected_manifest),
        bundle.coder_secret_inventory_sha256,
        bundle.legacy_workspace_managed_fingerprints_sha256,
        bundle.preexisting_cloudflare_sha256,
        bundle.post_install_cloudflare_sha256,
        bundle.snapshot_evidence,
    )
