from __future__ import annotations

from dataclasses import replace
from typing import assert_never

from dokploy_wizard.dokploy.coder_migration_receipt_types import MigrationReceipt
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationBlockedError
from dokploy_wizard.dokploy.coder_template_migration_checkpoint import (
    checkpoint_step,
    current_step,
)
from dokploy_wizard.dokploy.coder_template_migration_fingerprints import (
    inventory_sha256,
    target_fingerprint,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationDependencies,
    TemplateMigrationTarget,
)
from dokploy_wizard.dokploy.coder_template_migration_mutations import TemplateMutationExecutor
from dokploy_wizard.dokploy.coder_template_migration_plan import build_migration_receipt
from dokploy_wizard.dokploy.coder_template_migration_retirement import RetirementExecutor

_RETAINED_NAMES = (
    "ubuntu-vscode-opencode-pi",
    "ubuntu-vscode-opencode-web",
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-kdense-byok",
)
_RETIRED_NAMES = frozenset(("ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"))
_MANAGED_NAMES = frozenset((*_RETAINED_NAMES, "ubuntu-vscode", *_RETIRED_NAMES))


class CoderTemplateMigration:
    def __init__(self, dependencies: TemplateMigrationDependencies) -> None:
        self._dependencies = dependencies

    def run(
        self, organization_id: str, targets: tuple[TemplateMigrationTarget, ...]
    ) -> MigrationReceipt:
        desired_fingerprint = target_fingerprint(targets)
        receipt = self._dependencies.receipt_store.load()
        if receipt is None:
            planned = build_migration_receipt(
                self._dependencies.api,
                self._dependencies.pusher,
                organization_id,
                targets,
                self._dependencies.clock(),
            )
            self._dependencies.crash_hook.hit("before_journal")
            receipt = self._dependencies.receipt_store.create(planned)
        elif receipt.desired_fingerprint != desired_fingerprint:
            raise CoderMigrationBlockedError(
                "migration receipt targets do not match current inputs"
            )
        if receipt.status == "completed":
            self._verify_final(receipt, targets, checkpoint=False)
            return receipt
        if receipt.status in {"blocked", "failed"}:
            raise CoderMigrationBlockedError("migration receipt is terminal and not recoverable")
        inventory_step = current_step(receipt, "inventory")
        if inventory_step.status == "pending":
            inventory_step = replace(
                inventory_step,
                status="verified",
                post_inventory_sha256=receipt.pre_inventory_sha256,
                updated_at=self._dependencies.clock(),
            )
            receipt = checkpoint_step(self._dependencies, receipt, inventory_step, "running")
        mutations = TemplateMutationExecutor(self._dependencies, targets)
        retirement = RetirementExecutor(self._dependencies)
        for planned_step in receipt.steps:
            step = current_step(receipt, planned_step.step_id)
            match step.kind:
                case "inventory" | "verify":
                    continue
                case "rename_template":
                    receipt = mutations.rename(receipt, step)
                case "push_template":
                    receipt = mutations.push(receipt, step)
                case "delete_workspace":
                    receipt = retirement.workspace(receipt, step)
                case "delete_template":
                    receipt = retirement.template(receipt, step)
                case unreachable:
                    assert_never(unreachable)
        return self._verify_final(receipt, targets, checkpoint=True)

    def _verify_final(
        self,
        receipt: MigrationReceipt,
        targets: tuple[TemplateMigrationTarget, ...],
        *,
        checkpoint: bool,
    ) -> MigrationReceipt:
        organization_id = self._organization_id(receipt)
        templates = self._dependencies.api.list_templates(organization_id)
        managed_names = {template.name for template in templates if template.name in _MANAGED_NAMES}
        if managed_names != set(_RETAINED_NAMES):
            raise CoderMigrationBlockedError("final retained Coder template names are not exact")
        templates_by_name = {template.name: template for template in templates}
        for target in targets:
            template = templates_by_name.get(target.name)
            if template is None:
                raise CoderMigrationBlockedError("retained Coder template is absent")
            push_step = current_step(receipt, f"push-template-{target.name}")
            if push_step.template_id is not None and template.id != push_step.template_id:
                raise CoderMigrationBlockedError("retained Coder template UUID changed")
            versions = self._dependencies.pusher.version_names(target.name)
            if (
                versions.count(target.version_name) != 1
                or self._dependencies.pusher.active_version_name(target.name)
                != target.version_name
            ):
                raise CoderMigrationBlockedError("retained template active digest is not exact")
        workspaces = self._dependencies.api.list_workspaces()
        retired_ids = {
            step.template_id
            for step in receipt.steps
            if step.kind == "delete_template" and step.template_id is not None
        }
        if any(workspace.template_id in retired_ids for workspace in workspaces):
            raise CoderMigrationBlockedError("retired template still has dependent workspaces")
        if any(step.status != "verified" for step in receipt.steps if step.kind != "verify"):
            raise CoderMigrationBlockedError("migration receipt has an unverified mutation")
        if not checkpoint:
            return receipt
        final_hash = inventory_sha256(templates, workspaces, {})
        verify = current_step(receipt, "verify")
        verified = replace(
            verify,
            status="verified",
            post_inventory_sha256=final_hash,
            updated_at=self._dependencies.clock(),
        )
        return checkpoint_step(
            self._dependencies, receipt, verified, "completed", final_hash
        )

    @staticmethod
    def _organization_id(receipt: MigrationReceipt) -> str:
        values = {
            step.organization_id
            for step in receipt.steps
            if step.organization_id is not None
        }
        if len(values) != 1:
            raise CoderMigrationBlockedError("migration organization binding is ambiguous")
        return str(next(iter(values)))


__all__ = (
    "CoderTemplateMigration",
    "TemplateMigrationDependencies",
    "TemplateMigrationTarget",
)
