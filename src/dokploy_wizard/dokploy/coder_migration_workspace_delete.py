from __future__ import annotations

from dataclasses import replace
from typing import NoReturn, assert_never

from dokploy_wizard.dokploy.coder_migration_inventory import (
    WorkspaceDeletionSnapshot,
    workspace_deletion_snapshot,
    workspace_recovery_snapshot,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    new_migration_receipt,
    new_migration_step,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationApi,
    CoderMigrationBlockedError,
    CoderMigrationCrashHook,
    CoderMigrationDependencies,
    WorkspaceDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_workspace_recovery import (
    block_workspace_delete,
    checkpoint_workspace_delete,
    poll_workspace_delete,
    recover_workspace_delete,
    workspace_delete_request_hash,
    workspace_delete_response_hash,
)


class CoderMigrationOperations:
    def __init__(self, dependencies: CoderMigrationDependencies) -> None:
        self._dependencies = dependencies

    def delete_workspace(self, request: WorkspaceDeletionRequest) -> MigrationReceipt:
        initial = workspace_deletion_snapshot(self._dependencies.api, request.workspace_id)
        _require_exactly_stopped(initial)
        receipt, intent = _workspace_intent(request, initial, self._dependencies.clock())
        receipt = self._create(receipt)
        rechecked = workspace_deletion_snapshot(self._dependencies.api, request.workspace_id)
        if rechecked != initial:
            self._block(
                receipt,
                intent,
                "workspace inventory changed before delete",
                rechecked.inventory_sha256,
            )
        self._dependencies.crash_hook.hit("before_request")
        try:
            submitted = self._dependencies.api.submit_workspace_delete(request.workspace_id)
        except CoderApiError as error:
            return self._handle_api_error(receipt, intent, request.workspace_id, error)
        self._dependencies.crash_hook.hit("after_response")
        submitted_step = replace(
            intent,
            status="submitted",
            request_sha256=workspace_delete_request_hash(request.workspace_id),
            response_sha256=workspace_delete_response_hash(request.workspace_id, submitted),
            submitted_delete_build_id=submitted.id,
            submitted_delete_build_number=submitted.build_number,
            updated_at=self._dependencies.clock(),
        )
        receipt = checkpoint_workspace_delete(
            self._dependencies, receipt, "running", submitted_step, None
        )
        self._dependencies.crash_hook.hit("after_submit_journal")
        return poll_workspace_delete(self._dependencies, receipt, submitted_step)

    def recover_workspace_delete(self) -> MigrationReceipt:
        return recover_workspace_delete(self._dependencies)

    def _create(self, receipt: MigrationReceipt) -> MigrationReceipt:
        self._dependencies.crash_hook.hit("before_journal")
        self._dependencies.crash_hook.hit("before_checkpoint")
        return self._dependencies.receipt_store.create(receipt)

    def _handle_api_error(
        self,
        receipt: MigrationReceipt,
        step: MigrationStep,
        workspace_id: str,
        error: CoderApiError,
    ) -> MigrationReceipt:
        match error.status:
            case 404:
                return self.recover_workspace_delete()
            case 409:
                observed = workspace_recovery_snapshot(self._dependencies.api, workspace_id)
                self._block(
                    receipt,
                    step,
                    "Coder delete returned HTTP 409",
                    observed.inventory_sha256,
                )
            case _:
                failed = replace(
                    step,
                    status="failed",
                    error=str(error),
                    updated_at=self._dependencies.clock(),
                )
                return checkpoint_workspace_delete(
                    self._dependencies, receipt, "failed", failed, None
                )

    def _block(
        self,
        receipt: MigrationReceipt,
        step: MigrationStep,
        reason: str,
        inventory_sha256: str,
    ) -> NoReturn:
        block_workspace_delete(self._dependencies, receipt, step, reason, inventory_sha256)


def _require_exactly_stopped(snapshot: WorkspaceDeletionSnapshot) -> None:
    match snapshot.workspace.latest_build.status:
        case "stopped":
            return
        case (
            "pending"
            | "starting"
            | "running"
            | "stopping"
            | "failed"
            | "canceling"
            | "canceled"
            | "deleting"
            | "deleted"
        ):
            raise CoderMigrationBlockedError("workspace latest build is not exactly stopped")
        case unreachable:
            assert_never(unreachable)


def _workspace_intent(
    request: WorkspaceDeletionRequest, snapshot: WorkspaceDeletionSnapshot, created_at: str
) -> tuple[MigrationReceipt, MigrationStep]:
    workspace = snapshot.workspace
    step = replace(
        new_migration_step(
            step_id=f"delete-workspace-{workspace.id}",
            kind="delete_workspace",
            status="intent",
            created_at=created_at,
        ),
        template_id=workspace.template_id,
        workspace_id=workspace.id,
        name=workspace.name,
        latest_build_id=workspace.latest_build.id,
        latest_build_number=workspace.latest_build.build_number,
        latest_build_status=workspace.latest_build.status,
        pre_delete_build_sequence=snapshot.builds,
        stopped=True,
        desired_fingerprint=request.desired_fingerprint,
        pre_inventory_sha256=snapshot.inventory_sha256,
    )
    receipt = new_migration_receipt(
        operation_id=request.operation_id,
        desired_fingerprint=request.desired_fingerprint,
        pre_inventory_sha256=snapshot.inventory_sha256,
        created_at=created_at,
        step=step,
    )
    return receipt, step


__all__ = [
    "CoderMigrationApi",
    "CoderMigrationBlockedError",
    "CoderMigrationCrashHook",
    "CoderMigrationDependencies",
    "CoderMigrationOperations",
    "WorkspaceDeletionRequest",
]
