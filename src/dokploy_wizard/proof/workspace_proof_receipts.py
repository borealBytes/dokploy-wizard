"""Typed, atomic receipts for crash-resumable proof workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, assert_never

from dokploy_wizard.state.upgrade_io import atomic_json, read_json


class WorkspaceProofPhase(StrEnum):
    """The durable phase of a proof workspace's lifecycle."""

    PLANNED = "planned"
    CREATED = "created"
    TESTED = "tested"
    STOPPED = "stopped"
    DELETED = "deleted"


class WorkspaceProofError(RuntimeError):
    """A proof workspace receipt is unsafe to continue."""

    pass


@dataclass(frozen=True, slots=True)
class WorkspaceProofRef:
    """The provider reference required to manage an owned proof workspace."""

    workspace_id: str
    workspace_name: str
    template_name: str


class WorkspaceProofClient(Protocol):
    """The minimal provider operations needed for a workspace proof."""

    def find(self, workspace_name: str) -> tuple[WorkspaceProofRef, ...]: ...

    def create(self, *, template_name: str, workspace_name: str) -> WorkspaceProofRef: ...

    def test(self, workspace: WorkspaceProofRef) -> None: ...

    def stop(self, workspace: WorkspaceProofRef) -> None: ...

    def delete(self, workspace: WorkspaceProofRef) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkspaceProofReceipt:
    """An atomic receipt that makes every destructive lifecycle step resumable."""

    phase: WorkspaceProofPhase
    template_name: str
    workspace_name: str
    workspace: WorkspaceProofRef | None

    def __post_init__(self) -> None:
        match self.phase:
            case WorkspaceProofPhase.PLANNED:
                if self.workspace is not None:
                    raise WorkspaceProofError(
                        "Planned workspace proof cannot have a provider reference."
                    )
            case (
                WorkspaceProofPhase.CREATED
                | WorkspaceProofPhase.TESTED
                | WorkspaceProofPhase.STOPPED
                | WorkspaceProofPhase.DELETED
            ):
                if self.workspace is None:
                    raise WorkspaceProofError(
                        "Completed workspace proof phase requires a provider reference."
                    )
            case unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class WorkspaceProofStore:
    """Mode-0600 state storage for a single proof workspace lifecycle."""

    state_dir: Path

    @property
    def receipt_path(self) -> Path:
        return self.state_dir / "workspace-proof-receipt.json"

    def load(self) -> WorkspaceProofReceipt | None:
        """Load an existing receipt after validating its exact typed schema."""

        if not self.receipt_path.exists():
            return None
        payload = read_json(self.receipt_path)
        phase_name = _required_string(payload, "phase")
        try:
            phase = WorkspaceProofPhase(phase_name)
        except ValueError as error:
            raise WorkspaceProofError("Workspace proof receipt has an unknown phase.") from error
        template_name = _required_string(payload, "template_name")
        workspace_name = _required_string(payload, "workspace_name")
        workspace_id = payload.get("workspace_id")
        match phase:
            case WorkspaceProofPhase.PLANNED:
                if workspace_id is not None:
                    raise WorkspaceProofError(
                        "Planned workspace proof receipt has a provider identifier."
                    )
                workspace = None
            case (
                WorkspaceProofPhase.CREATED
                | WorkspaceProofPhase.TESTED
                | WorkspaceProofPhase.STOPPED
                | WorkspaceProofPhase.DELETED
            ):
                if not isinstance(workspace_id, str) or not workspace_id:
                    raise WorkspaceProofError(
                        "Workspace proof receipt lacks a provider identifier."
                    )
                workspace = WorkspaceProofRef(workspace_id, workspace_name, template_name)
            case unreachable:
                assert_never(unreachable)
        return WorkspaceProofReceipt(phase, template_name, workspace_name, workspace)

    def save(self, receipt: WorkspaceProofReceipt) -> None:
        """Publish a phase transition before the following provider operation."""

        workspace_id = receipt.workspace.workspace_id if receipt.workspace is not None else None
        atomic_json(
            self.receipt_path,
            {
                "phase": receipt.phase.value,
                "template_name": receipt.template_name,
                "workspace_name": receipt.workspace_name,
                "workspace_id": workspace_id,
            },
        )


def receipt_workspace(receipt: WorkspaceProofReceipt) -> WorkspaceProofRef:
    """Return the required provider reference for a non-planned receipt."""

    workspace = receipt.workspace
    if workspace is None:
        raise WorkspaceProofError("Workspace proof receipt is missing its provider reference.")
    return workspace


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise WorkspaceProofError(f"Workspace proof receipt is missing {key}.")
    return value
