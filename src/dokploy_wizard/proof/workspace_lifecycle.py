"""Crash-resumable proof workspaces with durable cleanup receipts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from typing import assert_never

from dokploy_wizard.proof.mutation_registry import (
    MutationBackend,
    MutationKind,
    ProofMutationRecorder,
)
from dokploy_wizard.proof.workspace_proof_receipts import (
    WorkspaceProofClient,
    WorkspaceProofError,
    WorkspaceProofPhase,
    WorkspaceProofReceipt,
    WorkspaceProofRef,
    WorkspaceProofStore,
    receipt_workspace,
)

__all__ = [
    "WorkspaceProofClient",
    "WorkspaceProofError",
    "WorkspaceProofPhase",
    "WorkspaceProofReceipt",
    "WorkspaceProofRef",
    "WorkspaceProofStore",
    "run_workspace_proof",
]


def run_workspace_proof(
    *,
    client: WorkspaceProofClient,
    store: WorkspaceProofStore,
    template_name: str,
    mutation_recorder: ProofMutationRecorder,
) -> WorkspaceProofReceipt:
    """Create, test, stop, and delete a workspace, resuming from its receipt."""

    receipt = store.load()
    if receipt is None:
        receipt = WorkspaceProofReceipt(
            phase=WorkspaceProofPhase.PLANNED,
            template_name=template_name,
            workspace_name=_workspace_name(template_name),
            workspace=None,
        )
        store.save(receipt)
    if receipt.template_name != template_name:
        raise WorkspaceProofError("Workspace proof receipt belongs to a different template.")
    while receipt.phase is not WorkspaceProofPhase.DELETED:
        match receipt.phase:
            case WorkspaceProofPhase.PLANNED:
                receipt = _recover_or_create(client, receipt, mutation_recorder)
                store.save(receipt)
            case WorkspaceProofPhase.CREATED:
                workspace = receipt_workspace(receipt)
                _run_workspace_mutation(mutation_recorder, lambda: client.test(workspace))
                receipt = replace(receipt, phase=WorkspaceProofPhase.TESTED)
                store.save(receipt)
            case WorkspaceProofPhase.TESTED:
                workspace = receipt_workspace(receipt)
                _run_workspace_mutation(mutation_recorder, lambda: client.stop(workspace))
                receipt = replace(receipt, phase=WorkspaceProofPhase.STOPPED)
                store.save(receipt)
            case WorkspaceProofPhase.STOPPED:
                receipt = _recover_or_delete(client, receipt, mutation_recorder)
                store.save(receipt)
            case unreachable:
                assert_never(unreachable)
    return receipt


def _recover_or_create(
    client: WorkspaceProofClient,
    receipt: WorkspaceProofReceipt,
    mutation_recorder: ProofMutationRecorder,
) -> WorkspaceProofReceipt:
    matches = client.find(receipt.workspace_name)
    match matches:
        case ():
            workspace = _created_workspace(
                client,
                receipt.template_name,
                receipt.workspace_name,
                mutation_recorder,
            )
        case (workspace,):
            if workspace.template_name != receipt.template_name:
                raise WorkspaceProofError("Found proof workspace belongs to a different template.")
        case _:
            raise WorkspaceProofError(
                "Proof workspace name resolves to more than one provider workspace."
            )
    if workspace.workspace_name != receipt.workspace_name:
        raise WorkspaceProofError("Provider created a proof workspace with an unexpected name.")
    if workspace.template_name != receipt.template_name:
        raise WorkspaceProofError("Provider created a proof workspace from an unexpected template.")
    return replace(receipt, phase=WorkspaceProofPhase.CREATED, workspace=workspace)


def _recover_or_delete(
    client: WorkspaceProofClient,
    receipt: WorkspaceProofReceipt,
    mutation_recorder: ProofMutationRecorder,
) -> WorkspaceProofReceipt:
    workspace = receipt_workspace(receipt)
    matches = client.find(receipt.workspace_name)
    match matches:
        case ():
            return replace(receipt, phase=WorkspaceProofPhase.DELETED)
        case (current,):
            if current.workspace_id != workspace.workspace_id:
                raise WorkspaceProofError(
                    "Found proof workspace has an unexpected provider identifier."
                )
            _run_workspace_mutation(mutation_recorder, lambda: client.delete(current))
            return replace(receipt, phase=WorkspaceProofPhase.DELETED)
        case _:
            raise WorkspaceProofError(
                "Proof workspace name resolves to more than one provider workspace."
            )


def _created_workspace(
    client: WorkspaceProofClient,
    template_name: str,
    workspace_name: str,
    mutation_recorder: ProofMutationRecorder,
) -> WorkspaceProofRef:
    return mutation_recorder.execute_result(
        backend=MutationBackend.WORKSPACE_PROOF.value,
        kind=MutationKind.WORKSPACE_PROOF,
        action=lambda: client.create(
            template_name=template_name,
            workspace_name=workspace_name,
        ),
    )


def _run_workspace_mutation(
    mutation_recorder: ProofMutationRecorder,
    action: Callable[[], None],
) -> None:
    mutation_recorder.execute(
        backend=MutationBackend.WORKSPACE_PROOF.value,
        kind=MutationKind.WORKSPACE_PROOF,
        action=action,
    )


def _workspace_name(template_name: str) -> str:
    digest = sha256(template_name.encode()).hexdigest()[:16]
    return f"dokploy-wizard-proof-{digest}"
