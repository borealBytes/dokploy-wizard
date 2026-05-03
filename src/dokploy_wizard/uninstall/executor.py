"""Execution helpers for safe uninstall flows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dokploy_wizard.state import (
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    RawEnvInput,
    clear_state_documents,
    write_applied_checkpoint,
    write_ownership_ledger,
)
from dokploy_wizard.tailscale import TAILSCALE_NODE_RESOURCE_TYPE
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    UninstallPlan,
    compute_remaining_completed_steps,
)


class UninstallExecutionError(RuntimeError):
    """Raised when a resource deletion fails during uninstall."""


class UninstallBackend(Protocol):
    def delete(self, deletion: PlannedDeletion) -> None: ...


class ShellUninstallBackend:
    """Deterministic default backend for ledger-driven teardown execution."""

    def __init__(self, raw_input: RawEnvInput) -> None:
        values = raw_input.values
        self._failing_types = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_TYPES", "").split(",")
            if item.strip() != ""
        }
        self._failing_ids = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_IDS", "").split(",")
            if item.strip() != ""
        }

    def delete(self, deletion: PlannedDeletion) -> None:
        if deletion.resource.resource_type == TAILSCALE_NODE_RESOURCE_TYPE:
            result = subprocess.run(
                ["tailscale", "down"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                msg = "tailscale down failed during uninstall"
                if stderr:
                    msg = f"{msg}: {stderr}"
                raise UninstallExecutionError(msg)
            return
        if deletion.resource.resource_type in self._failing_types:
            raise UninstallExecutionError(
                "Simulated uninstall failure for resource type "
                f"'{deletion.resource.resource_type}'."
            )
        if deletion.resource.resource_id in self._failing_ids:
            raise UninstallExecutionError(
                f"Simulated uninstall failure for resource id '{deletion.resource.resource_id}'."
            )


@dataclass(frozen=True)
class UninstallExecutionResult:
    deleted_resources: tuple[PlannedDeletion, ...]
    remaining_completed_steps: tuple[str, ...]
    state_cleared: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_resources": [item.to_dict() for item in self.deleted_resources],
            "remaining_completed_steps": list(self.remaining_completed_steps),
            "state_cleared": self.state_cleared,
        }


def execute_uninstall_plan(
    *,
    state_dir: Path,
    raw_input: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    plan: UninstallPlan,
    backend: UninstallBackend,
    dry_run: bool,
) -> UninstallExecutionResult:
    if dry_run:
        return UninstallExecutionResult(
            deleted_resources=plan.deletions,
            remaining_completed_steps=_cap_completed_steps(
                compute_remaining_completed_steps(
                    desired_state=desired_state,
                    raw_input=raw_input,
                    ownership_ledger=ownership_ledger,
                ),
                plan.completed_steps_ceiling,
            ),
            state_cleared=False,
        )

    deleted_resources: list[PlannedDeletion] = []
    current_ledger = ownership_ledger
    remaining_completed_steps = _cap_completed_steps(
        compute_remaining_completed_steps(
            desired_state=desired_state,
            raw_input=raw_input,
            ownership_ledger=current_ledger,
        ),
        plan.completed_steps_ceiling,
    )
    for deletion in plan.deletions:
        backend.delete(deletion)
        deleted_resources.append(deletion)
        current_ledger = OwnershipLedger(
            format_version=current_ledger.format_version,
            resources=tuple(
                resource
                for resource in current_ledger.resources
                if not (
                    resource.resource_type == deletion.resource.resource_type
                    and resource.resource_id == deletion.resource.resource_id
                    and resource.scope == deletion.resource.scope
                )
            ),
        )
        write_ownership_ledger(state_dir, current_ledger)
        remaining_completed_steps = _cap_completed_steps(
            compute_remaining_completed_steps(
                desired_state=desired_state,
                raw_input=raw_input,
                ownership_ledger=current_ledger,
            ),
            plan.completed_steps_ceiling,
        )
        write_applied_checkpoint(
            state_dir,
            AppliedStateCheckpoint(
                format_version=desired_state.format_version,
                desired_state_fingerprint=desired_state.fingerprint(),
                completed_steps=remaining_completed_steps,
                lifecycle_checkpoint_contract_version=LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
            ),
        )

    state_cleared = not current_ledger.resources
    if state_cleared:
        clear_state_documents(state_dir)

    return UninstallExecutionResult(
        deleted_resources=tuple(deleted_resources),
        remaining_completed_steps=remaining_completed_steps,
        state_cleared=state_cleared,
    )


def _cap_completed_steps(
    completed_steps: tuple[str, ...], ceiling: tuple[str, ...] | None
) -> tuple[str, ...]:
    if ceiling is None or len(completed_steps) <= len(ceiling):
        return completed_steps
    return ceiling
