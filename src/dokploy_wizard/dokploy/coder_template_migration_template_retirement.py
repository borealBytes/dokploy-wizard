from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.dokploy.coder_migration_inventory import (
    TemplateDeletionSnapshot,
    template_deletion_snapshot,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
)
from dokploy_wizard.dokploy.coder_migration_template_recovery import (
    template_delete_request_hash,
    template_delete_response_hash,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError
from dokploy_wizard.dokploy.coder_template_migration_checkpoint import (
    block_step,
    checkpoint_step,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationDependencies,
)


class TemplateRetirementExecutor:
    def __init__(self, dependencies: TemplateMigrationDependencies) -> None:
        self._dependencies = dependencies

    def run(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        if step.status == "verified":
            self._require_deleted(receipt, step)
            return receipt
        if step.status == "submitted":
            return self._verify(receipt, step)
        if step.status != "intent":
            return block_step(
                self._dependencies,
                receipt,
                step,
                "template delete receipt is not recoverable",
            )
        observed = self._snapshot(receipt, step)
        if observed.template is None:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "unreceipted template disappearance cannot prove deletion",
            )
        self._require_deletable(receipt, step)
        self._dependencies.crash_hook.hit("before_request")
        try:
            response = self._dependencies.api.delete_template(self._template_id(receipt, step))
        except CoderApiError as error:
            reason = (
                "unreceipted template delete HTTP 404 cannot prove deletion"
                if error.status == 404
                else f"Coder template delete failed with HTTP {error.status}"
            )
            return block_step(self._dependencies, receipt, step, reason)
        submitted = self._submitted(step, response)
        receipt = checkpoint_step(self._dependencies, receipt, submitted, "running")
        self._dependencies.crash_hook.hit("after_response")
        return self._verify(receipt, submitted)

    def _submitted(self, step: MigrationStep, response: bytes) -> MigrationStep:
        template_id = str(step.template_id)
        return replace(
            step,
            status="submitted",
            request_sha256=template_delete_request_hash(template_id),
            response_sha256=template_delete_response_hash(template_id, response),
            updated_at=self._dependencies.clock(),
        )

    def _verify(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        observed = self._snapshot(receipt, step)
        if observed.template is not None or observed.dependent_workspace_ids:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "template deletion lacks complete authenticated absence proof",
            )
        verified = replace(
            step,
            status="verified",
            post_inventory_sha256=observed.inventory_sha256,
            updated_at=self._dependencies.clock(),
        )
        return checkpoint_step(self._dependencies, receipt, verified, "running")

    def _require_deletable(self, receipt: MigrationReceipt, step: MigrationStep) -> None:
        first = self._snapshot(receipt, step)
        second = self._snapshot(receipt, step)
        if first != second or first.template is None or first.dependent_workspace_ids:
            block_step(
                self._dependencies,
                receipt,
                step,
                "retired template still has dependents or drift",
            )

    def _require_deleted(self, receipt: MigrationReceipt, step: MigrationStep) -> None:
        observed = self._snapshot(receipt, step)
        if observed.template is not None or observed.dependent_workspace_ids:
            block_step(
                self._dependencies, receipt, step, "verified template deletion drifted"
            )

    def _snapshot(
        self, receipt: MigrationReceipt, step: MigrationStep
    ) -> TemplateDeletionSnapshot:
        if step.organization_id is None:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "template organization binding is absent",
            )
        return template_deletion_snapshot(
            self._dependencies.api,
            str(step.organization_id),
            self._template_id(receipt, step),
        )

    def _template_id(self, receipt: MigrationReceipt, step: MigrationStep) -> str:
        if step.template_id is None:
            return block_step(
                self._dependencies, receipt, step, "template UUID binding is absent"
            )
        return str(step.template_id)
