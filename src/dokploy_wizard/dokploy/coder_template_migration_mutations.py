from __future__ import annotations

from dataclasses import replace
from typing import assert_never

from dokploy_wizard.dokploy.coder_migration_mutation_hash import (
    mutation_request_sha256,
    mutation_response_sha256,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import MigrationReceipt, MigrationStep
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError, CoderTemplate, JsonValue
from dokploy_wizard.dokploy.coder_template_migration_checkpoint import (
    block_step,
    checkpoint_step,
    current_step,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationDependencies,
    TemplateMigrationTarget,
)
from dokploy_wizard.dokploy.coder_template_migration_push import TemplatePushExecutor


class TemplateMutationExecutor:
    def __init__(
        self,
        dependencies: TemplateMigrationDependencies,
        targets: tuple[TemplateMigrationTarget, ...],
    ) -> None:
        self._dependencies = dependencies
        self._push = TemplatePushExecutor(dependencies, targets)

    def rename(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        if step.status == "verified":
            self._require_renamed(receipt, step)
            return receipt
        if step.status not in {"intent", "submitted"}:
            return block_step(
                self._dependencies, receipt, step, "rename receipt is not recoverable"
            )
        template = self._template_by_id(receipt, step)
        if template.name != step.name:
            if template.name != "ubuntu-vscode":
                return block_step(
                    self._dependencies, receipt, step, "primary template UUID name drifted"
                )
            self._require_name_absent(receipt, step)
            self._dependencies.crash_hook.hit("before_request")
            try:
                template = self._dependencies.api.rename_template(str(template.id), step.name or "")
            except CoderApiError as error:
                return block_step(
                    self._dependencies,
                    receipt,
                    step,
                    f"Coder rename failed with HTTP {error.status}",
                )
            self._dependencies.crash_hook.hit("after_response")
        submitted = self._rename_submitted(step, template)
        if step.status == "intent":
            receipt = checkpoint_step(self._dependencies, receipt, submitted, "running")
        current = current_step(receipt, step.step_id)
        self._require_renamed(receipt, current)
        verified = replace(
            current,
            status="verified",
            post_inventory_sha256=current.pre_inventory_sha256,
            updated_at=self._dependencies.clock(),
        )
        return checkpoint_step(self._dependencies, receipt, verified, "running")

    def push(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        return self._push.run(receipt, step)

    def _template_by_id(self, receipt: MigrationReceipt, step: MigrationStep) -> CoderTemplate:
        templates = self._dependencies.api.list_templates(str(step.organization_id))
        matches = tuple(template for template in templates if template.id == step.template_id)
        if len(matches) != 1:
            return block_step(
                self._dependencies, receipt, step, "rename UUID match is absent or ambiguous"
            )
        return matches[0]

    def _require_name_absent(self, receipt: MigrationReceipt, step: MigrationStep) -> None:
        templates = self._dependencies.api.list_templates(str(step.organization_id))
        if any(
            template.name == step.name and template.id != step.template_id for template in templates
        ):
            block_step(self._dependencies, receipt, step, "rename target name collides")

    def _require_renamed(self, receipt: MigrationReceipt, step: MigrationStep) -> None:
        template = self._template_by_id(receipt, step)
        if template.name != step.name:
            block_step(self._dependencies, receipt, step, "renamed template identity is not exact")
        self._require_name_absent(receipt, step)

    def _rename_submitted(self, step: MigrationStep, template: CoderTemplate) -> MigrationStep:
        if template.id != step.template_id or template.name != step.name:
            raise ValueError("rename response does not match the receipted UUID and name")
        body: dict[str, JsonValue] = {"name": template.name}
        response: dict[str, JsonValue] = {
            "id": template.id,
            "name": template.name,
            "organization_id": template.organization_id,
        }
        return replace(
            step,
            status="submitted",
            request_sha256=mutation_request_sha256(
                "PATCH", f"/api/v2/templates/{template.id}", body
            ),
            response_sha256=mutation_response_sha256(
                "PATCH", f"/api/v2/templates/{template.id}", body, response
            ),
            updated_at=self._dependencies.clock(),
        )

def require_recoverable_step(step: MigrationStep) -> None:
    match step.status:
        case "intent" | "submitted" | "verified":
            return
        case "pending" | "blocked" | "failed":
            raise ValueError("migration mutation step is not recoverable")
        case unreachable:
            assert_never(unreachable)
