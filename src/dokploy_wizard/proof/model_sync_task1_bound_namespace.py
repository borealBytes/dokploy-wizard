"""Reconstruct the exact Task 1 namespace from hash-verified external evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_namespace import build_proof_namespace
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
    project_task1_desired_state,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1
from dokploy_wizard.proof.model_sync_task1_evidence_schema import Task1ProofContextEvidenceV1
from dokploy_wizard.proof.model_sync_task1_evidence_validation import (
    verify_task1_external_evidence,
)
from dokploy_wizard.state import parse_env_file, resolve_desired_state

_MAX_CONTEXT_BYTES: Final = 256 * 1024


def resolve_bound_task1_namespace(
    evidence: Task1ProofContextEvidenceV1,
) -> proof.ProofNamespace:
    """Resolve the proof namespace from the accepted Task 1 upload and context files."""

    verify_task1_external_evidence(evidence)
    context_bytes, _mode = proof.read_bounded_regular_bytes(
        Path(evidence.context_path),
        _MAX_CONTEXT_BYTES,
        0o600,
    )
    context = Task1ProofContextV1.from_bytes(context_bytes)
    raw_env = parse_env_file(Path(evidence.upload_env_path))
    with activate_task1_proof_context(context):
        desired = project_task1_desired_state(resolve_desired_state(raw_env))
    return build_proof_namespace(raw_env.values, desired)
