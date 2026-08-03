from __future__ import annotations

from dataclasses import replace
from typing import NoReturn

from dokploy_wizard.dokploy.coder_migration_inventory import (
    TemplateDeletionSnapshot,
    template_deletion_snapshot,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    new_migration_receipt,
    new_migration_step,
)
from dokploy_wizard.dokploy.coder_migration_template_models import (
    CoderTemplateMigrationApi,
    TemplateDeletionDependencies,
    TemplateDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_template_recovery import (
    block_template_delete,
    checkpoint_template_delete,
    complete_template_delete,
    recover_template_delete,
    template_delete_request_hash,
    template_delete_response_hash,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError, parse_coder_id
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
    CoderMigrationCrashHook,
)


class CoderTemplateDeletionOperations:
    def __init__(self, dependencies: TemplateDeletionDependencies) -> None:
        self._dependencies = dependencies

    def delete_template(self, request: TemplateDeletionRequest) -> MigrationReceipt:
        initial = template_deletion_snapshot(
            self._dependencies.api, request.organization_id, request.template_id
        )
        if initial.template is None or initial.dependent_workspace_ids:
            raise CoderMigrationBlockedError("template is absent or has dependent workspaces")
        receipt, intent = _template_intent(request, initial, self._dependencies.clock())
        receipt = self._create(receipt)
        rechecked = template_deletion_snapshot(
            self._dependencies.api, request.organization_id, request.template_id
        )
        if rechecked != initial:
            self._block(receipt, intent, "template inventory changed before delete", rechecked)
        self._dependencies.crash_hook.hit("before_request")
        try:
            response = self._dependencies.api.delete_template(request.template_id)
        except CoderApiError as error:
            return self._handle_api_error(receipt, intent, error)
        self._dependencies.crash_hook.hit("after_response")
        submitted = replace(
            intent,
            status="submitted",
            request_sha256=template_delete_request_hash(request.template_id),
            response_sha256=template_delete_response_hash(request.template_id, response),
            updated_at=self._dependencies.clock(),
        )
        receipt = checkpoint_template_delete(
            self._dependencies, receipt, "running", submitted, None
        )
        self._dependencies.crash_hook.hit("after_submit_journal")
        return complete_template_delete(self._dependencies, receipt, submitted)

    def recover_template_delete(self) -> MigrationReceipt:
        return recover_template_delete(self._dependencies)

    def _create(self, receipt: MigrationReceipt) -> MigrationReceipt:
        self._dependencies.crash_hook.hit("before_journal")
        self._dependencies.crash_hook.hit("before_checkpoint")
        return self._dependencies.receipt_store.create(receipt)

    def _handle_api_error(
        self, receipt: MigrationReceipt, step: MigrationStep, error: CoderApiError
    ) -> MigrationReceipt:
        match error.status:
            case 404:
                return self.recover_template_delete()
            case 409:
                if step.organization_id is None or step.template_id is None:
                    raise CoderMigrationBlockedError("template deletion receipt lacks UUID binding")
                observed = template_deletion_snapshot(
                    self._dependencies.api, step.organization_id, step.template_id
                )
                self._block(receipt, step, "Coder template delete returned HTTP 409", observed)
            case _:
                failed = replace(
                    step,
                    status="failed",
                    error=str(error),
                    updated_at=self._dependencies.clock(),
                )
                return checkpoint_template_delete(
                    self._dependencies, receipt, "failed", failed, None
                )

    def _block(
        self,
        receipt: MigrationReceipt,
        step: MigrationStep,
        reason: str,
        observed: TemplateDeletionSnapshot,
    ) -> NoReturn:
        block_template_delete(self._dependencies, receipt, step, reason, observed)


def _template_intent(
    request: TemplateDeletionRequest, snapshot: TemplateDeletionSnapshot, created_at: str
) -> tuple[MigrationReceipt, MigrationStep]:
    step = replace(
        new_migration_step(
            step_id=f"delete-template-{request.template_id}",
            kind="delete_template",
            status="intent",
            created_at=created_at,
        ),
        template_id=parse_coder_id(request.template_id, "template_id"),
        organization_id=parse_coder_id(request.organization_id, "organization_id"),
        dependent_workspace_ids=snapshot.dependent_workspace_ids,
        dependent_inventory_sha256=snapshot.dependent_inventory_sha256,
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
    "CoderMigrationCrashHook",
    "CoderTemplateDeletionOperations",
    "CoderTemplateMigrationApi",
    "TemplateDeletionDependencies",
    "TemplateDeletionRequest",
]
