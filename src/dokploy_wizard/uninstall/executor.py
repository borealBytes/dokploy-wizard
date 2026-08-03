"""Execution helpers for safe uninstall flows."""

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.state import (
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    clear_state_documents,
    load_state_dir,
    write_applied_checkpoint,
    write_ownership_ledger,
)
from dokploy_wizard.state.shared_core_sync import (
    SYNC_SCHEDULE_RESOURCE_TYPE,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.coder_lifecycle import (
    CoderServiceDeletion,
    delete_with_coder_lifecycle,
    finish_coder_service,
    finish_orphaned_coder_teardown_for_plan,
)
from dokploy_wizard.uninstall.contracts import (
    RetainedSyncScheduleDisabler,
    UninstallBackend,
)
from dokploy_wizard.uninstall.errors import UninstallExecutionError as UninstallExecutionError
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    UninstallPlan,
    compute_remaining_completed_steps,
)
from dokploy_wizard.uninstall.result import UninstallExecutionResult, cap_completed_steps
from dokploy_wizard.uninstall.shell_backend import ShellUninstallBackend
from dokploy_wizard.uninstall.state_cleanup import (
    clear_sync_control_documents,
    require_sync_teardown_receipt,
)

__all__ = [
    "ShellUninstallBackend",
    "UninstallBackend",
    "UninstallExecutionError",
    "execute_uninstall_plan",
    "finish_coder_service",
]


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
            remaining_completed_steps=cap_completed_steps(
                compute_remaining_completed_steps(
                    desired_state=desired_state,
                    raw_input=raw_input,
                    ownership_ledger=ownership_ledger,
                ),
                plan.completed_steps_ceiling,
            ),
            state_cleared=False,
        )

    finish_orphaned_coder_teardown_for_plan(state_dir, plan)

    deleted_resources: list[PlannedDeletion] = []
    current_ledger = ownership_ledger
    existing_applied = load_state_dir(state_dir).applied_state
    retained_sync_applied = (
        None if existing_applied is None else existing_applied.opencode_go_sync
    )
    retained_sync_resources = tuple(
        resource
        for resource in plan.retained_resources
        if resource.resource_type == SYNC_SCHEDULE_RESOURCE_TYPE
    )
    for resource in retained_sync_resources:
        if (
            desired_state.opencode_go_sync is None
            or retained_sync_applied is None
            or not isinstance(backend, RetainedSyncScheduleDisabler)
        ):
            raise UninstallExecutionError(
                "Retained sync schedule cannot be disabled from incomplete state."
            )
        retained_sync_applied = backend.disable_sync_schedule(
            resource=resource,
            desired=desired_state.opencode_go_sync,
            applied=retained_sync_applied,
        )
    remaining_completed_steps = cap_completed_steps(
        compute_remaining_completed_steps(
            desired_state=desired_state,
            raw_input=raw_input,
            ownership_ledger=current_ledger,
        ),
        plan.completed_steps_ceiling,
    )
    if retained_sync_resources:
        write_applied_checkpoint(
            state_dir,
            AppliedStateCheckpoint(
                format_version=desired_state.format_version,
                desired_state_fingerprint=desired_state.fingerprint(),
                completed_steps=remaining_completed_steps,
                lifecycle_checkpoint_contract_version=LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
                runtime_images=desired_state.runtime_images,
                opencode_go_sync=retained_sync_applied,
            ),
        )
    for deletion in plan.deletions:
        coder_context = CoderServiceDeletion(state_dir, desired_state, deletion, backend, plan.mode)
        coder_transaction = delete_with_coder_lifecycle(coder_context)
        if deletion.resource.resource_type == SYNC_SCHEDULE_RESOURCE_TYPE:
            require_sync_teardown_receipt(state_dir, deletion.resource)
        else:
            _require_deletion_receipt(state_dir, deletion.resource)
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
        remaining_completed_steps = cap_completed_steps(
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
                runtime_images=desired_state.runtime_images,
                opencode_go_sync=retained_sync_applied,
            ),
        )
        if coder_transaction is not None:
            finish_coder_service(coder_transaction)

    state_cleared = not current_ledger.resources
    if state_cleared:
        if plan.mode == "destroy":
            clear_sync_control_documents(state_dir)
        clear_state_documents(state_dir)

    return UninstallExecutionResult(
        deleted_resources=tuple(deleted_resources),
        remaining_completed_steps=remaining_completed_steps,
        state_cleared=state_cleared,
    )


def _require_deletion_receipt(state_dir: Path, resource: OwnedResource) -> None:
    if UninstallAuthorityStore(state_dir).load_deletion(resource) is None:
        raise UninstallExecutionError(
            "Provider deletion must record a terminal deletion receipt before ledger removal."
        )
