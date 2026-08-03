"""Lease-bound strict rerun and retained-workspace proof composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.mutation_registry import ProofMutationRecorder, StrictMutationTotals
from dokploy_wizard.proof.strict_lease import strict_proof_lease
from dokploy_wizard.proof.workspace_lifecycle import (
    WorkspaceProofClient,
    WorkspaceProofPhase,
    WorkspaceProofStore,
    run_workspace_proof,
)

RETAINED_TEMPLATES = (
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-kdense-byok",
    "ubuntu-vscode-opencode-pi",
    "ubuntu-vscode-opencode-web",
)


StrictRerun = Callable[[ProofMutationRecorder], None]
WorkspaceClientFactory = Callable[[str], WorkspaceProofClient]


@dataclass(frozen=True, slots=True)
class StrictProofResult:
    template_names: tuple[str, ...]
    cleanup_complete: bool
    totals: StrictMutationTotals


def run_strict_proof(
    *,
    artifact_dir: Path,
    strict_rerun: StrictRerun,
    workspace_client: WorkspaceClientFactory,
) -> StrictProofResult:
    """Run the strict rerun and all retained workspace proofs under one OS lease."""

    if not artifact_dir.is_absolute():
        raise ValueError("Strict proof artifact directory must be absolute.")
    with strict_proof_lease(artifact_dir / ".strict-proof.lock") as recorder:
        strict_rerun(recorder)
        for template_name in RETAINED_TEMPLATES:
            receipt = run_workspace_proof(
                client=workspace_client(template_name),
                store=WorkspaceProofStore(artifact_dir / "workspace-proofs" / template_name),
                template_name=template_name,
                mutation_recorder=recorder,
            )
            if receipt.phase is not WorkspaceProofPhase.DELETED:
                raise RuntimeError("Workspace proof cleanup was incomplete.")
        recorder.assert_strict_zero()
        return StrictProofResult(RETAINED_TEMPLATES, True, recorder.totals)
