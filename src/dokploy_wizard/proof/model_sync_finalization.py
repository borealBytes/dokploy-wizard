"""Task 1 baseline payload construction before durable local finalization."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_lifecycle as lifecycle
from dokploy_wizard.proof import model_sync_preflight_evidence as identity
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_cloudflare_probe import cloudflare_snapshot_sha256_for_evidence
from dokploy_wizard.proof.model_sync_env import PreparedEnv
from dokploy_wizard.proof.model_sync_identity import ObservedResource
from dokploy_wizard.proof.model_sync_remote import RemoteProbe
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_finalization import (
    cloudflare_cleanup_payload,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_context import PreparedTask1ProofContext
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    Task1FinalizationPlan,
)


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
    post_install_cloudflare: tuple[ObservedResource, ...]
    task1_proof: PreparedTask1ProofContext | None = None
    post_install_snapshot: CloudflareSnapshotV1 | None = None
    cloudflare_cleanup_report: Task1CloudflareCleanupReport | None = None


def build_task1_finalization_plan(inputs: BaselineArtifactInputs) -> Task1FinalizationPlan:
    """Freeze redacted finalization inputs before remote cleanup can complete."""
    return Task1FinalizationPlan(
        source_base_commit=inputs.source_base_commit,
        proof_commit=inputs.proof_commit,
        host_identity_mode=inputs.host_identity_mode,
        payloads=build_baseline_payloads(inputs),
        images=dict(inputs.baseline.images),
        coder_secret_inventory_sha256=inputs.baseline.coder_secret_inventory_sha256,
        legacy_workspace_managed_fingerprints_sha256=(
            inputs.baseline.legacy_workspace_managed_fingerprints_sha256
        ),
        preexisting_cloudflare_sha256=inputs.host_a.preexisting_cloudflare_sha256,
    )


def build_baseline_payloads(inputs: BaselineArtifactInputs) -> dict[str, bytes]:
    """Build deterministic local artifact bytes from already-captured proof observations."""
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
    baseline_payload: dict[str, JsonValue] = {
        **inputs.baseline.payload,
        "preexisting_cloudflare": [
            resource.to_dict()
            for resource in inputs.host_a.inventory["cloudflare"]
            if resource.provenance == "preexisting_unowned"
        ],
        "post_install_cloudflare": _post_install_cloudflare_payload(inputs),
    }
    if inputs.cloudflare_cleanup_report is not None:
        baseline_payload.update(cloudflare_cleanup_payload(inputs.cloudflare_cleanup_report))
    payloads = {
        "baseline.json": proof.canonical_json_bytes(baseline_payload) + b"\n",
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
                    epoch_id=secrets.token_hex(32), expected_previous_sha256=None
                ),
            )
            payloads["single-host-lifecycle-baseline.json"] = receipt.to_bytes()
        case unexpected:
            assert_never(unexpected)
    return payloads


def post_install_cloudflare_sha256(inputs: BaselineArtifactInputs) -> str:
    """Return the canonical post-install Cloudflare evidence hash for direct finalization."""
    if inputs.post_install_snapshot is not None:
        return inputs.post_install_snapshot.snapshot_sha256
    return cloudflare_snapshot_sha256_for_evidence(inputs.post_install_cloudflare)


def _post_install_cloudflare_payload(inputs: BaselineArtifactInputs) -> JsonValue:
    if inputs.post_install_snapshot is not None:
        return require_mapping(
            json.loads(inputs.post_install_snapshot.to_bytes()), "post-install Cloudflare snapshot"
        )
    return [resource.to_dict() for resource in inputs.post_install_cloudflare]


from dokploy_wizard.proof.model_sync_finalization_execution import (  # noqa: E402
    finalize_baseline_artifacts as finalize_baseline_artifacts,
)
