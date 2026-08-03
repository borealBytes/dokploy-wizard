from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.dokploy.coder_migration_inventory import workspace_recovery_snapshot
from dokploy_wizard.dokploy.coder_migration_receipt_types import MigrationReceipt, MigrationStep
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError, CoderBuild
from dokploy_wizard.dokploy.coder_migration_workspace_recovery import (
    workspace_delete_request_hash,
    workspace_delete_response_hash,
)
from dokploy_wizard.dokploy.coder_template_migration_checkpoint import (
    block_step,
    checkpoint_step,
)
from dokploy_wizard.dokploy.coder_template_migration_models import TemplateMigrationDependencies
from dokploy_wizard.dokploy.coder_template_migration_template_retirement import (
    TemplateRetirementExecutor,
)


class RetirementExecutor:
    def __init__(self, dependencies: TemplateMigrationDependencies) -> None:
        self._dependencies = dependencies
        self._templates = TemplateRetirementExecutor(dependencies)

    def workspace(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        if step.status == "verified":
            self._require_deleted_workspace(receipt, step)
            return receipt
        if step.status == "submitted":
            return self._poll_workspace(receipt, step)
        if step.status != "intent":
            return block_step(
                self._dependencies, receipt, step, "workspace delete receipt is not recoverable"
            )
        observed = workspace_recovery_snapshot(
            self._dependencies.api, self._workspace_id(receipt, step)
        )
        later = observed.builds[len(step.pre_delete_build_sequence) :]
        if later:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "workspace delete recovery refuses an unreceipted transition",
            )
        self._require_stopped_workspace(receipt, step, observed.builds)
        self._dependencies.crash_hook.hit("before_request")
        try:
            build = self._dependencies.api.submit_workspace_delete(
                self._workspace_id(receipt, step)
            )
        except CoderApiError as error:
            reason = (
                "unreceipted workspace delete HTTP 404 cannot prove submission"
                if error.status == 404
                else f"Coder workspace delete failed with HTTP {error.status}"
            )
            return block_step(self._dependencies, receipt, step, reason)
        submitted = self._workspace_submitted(receipt, step, build)
        receipt = checkpoint_step(self._dependencies, receipt, submitted, "running")
        self._dependencies.crash_hook.hit("after_response")
        return self._poll_workspace(receipt, submitted)

    def template(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        return self._templates.run(receipt, step)

    def _poll_workspace(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        observed = None
        for attempt in range(self._dependencies.polling.attempts):
            observed = workspace_recovery_snapshot(
                self._dependencies.api, self._workspace_id(receipt, step)
            )
            later = observed.builds[len(step.pre_delete_build_sequence) :]
            submitted = self._recover_workspace_submission(receipt, step, later)
            if observed.workspace is None and later[0].status == "deleted":
                verified = replace(
                    submitted,
                    status="verified",
                    post_inventory_sha256=observed.inventory_sha256,
                    updated_at=self._dependencies.clock(),
                )
                return checkpoint_step(self._dependencies, receipt, verified, "running")
            if observed.workspace is None or later[0].status not in {"pending", "deleting"}:
                return block_step(
                    self._dependencies,
                    receipt,
                    submitted,
                    "workspace delete did not preserve one pending delete transition",
                )
            if attempt < self._dependencies.polling.attempts - 1:
                self._dependencies.polling.sleeper(self._dependencies.polling.delay_seconds)
        return block_step(
            self._dependencies, receipt, step, "workspace delete did not reach deleted"
        )

    def _require_stopped_workspace(
        self, receipt: MigrationReceipt, step: MigrationStep, builds: tuple[CoderBuild, ...]
    ) -> None:
        observed = workspace_recovery_snapshot(
            self._dependencies.api, self._workspace_id(receipt, step)
        )
        if (
            observed.workspace is None
            or observed.workspace.id != step.workspace_id
            or observed.workspace.template_id != step.template_id
            or observed.workspace.name != step.name
            or builds != step.pre_delete_build_sequence
            or observed.builds != step.pre_delete_build_sequence
            or observed.workspace.latest_build.status != "stopped"
        ):
            block_step(
                self._dependencies, receipt, step, "retired workspace is not exactly stopped"
            )

    def _recover_workspace_submission(
        self, receipt: MigrationReceipt, step: MigrationStep, later: tuple[CoderBuild, ...]
    ) -> MigrationStep:
        if len(later) != 1 or later[0].transition != "delete":
            return block_step(
                self._dependencies,
                receipt,
                step,
                "workspace delete recovery requires exactly one delete build",
            )
        self._require_workspace_submission(receipt, step, later[0])
        return step

    def _workspace_submitted(
        self, receipt: MigrationReceipt, step: MigrationStep, build: CoderBuild
    ) -> MigrationStep:
        self._require_workspace_submission(receipt, step, build)
        return replace(
            step,
            status="submitted",
            request_sha256=workspace_delete_request_hash(self._workspace_id(receipt, step)),
            response_sha256=workspace_delete_response_hash(
                self._workspace_id(receipt, step), build
            ),
            submitted_delete_build_id=build.id,
            submitted_delete_build_number=build.build_number,
            updated_at=self._dependencies.clock(),
        )

    def _require_workspace_submission(
        self, receipt: MigrationReceipt, step: MigrationStep, build: CoderBuild
    ) -> None:
        if (
            build.transition != "delete"
            or build.build_number != (step.latest_build_number or 0) + 1
            or build.status not in {"pending", "deleting", "deleted"}
        ):
            block_step(
                self._dependencies, receipt, step, "workspace delete build identity is invalid"
            )
        if step.submitted_delete_build_id is not None and (
            step.submitted_delete_build_id != build.id
            or step.submitted_delete_build_number != build.build_number
        ):
            block_step(
                self._dependencies, receipt, step, "workspace delete build identity changed"
            )

    def _require_deleted_workspace(self, receipt: MigrationReceipt, step: MigrationStep) -> None:
        observed = workspace_recovery_snapshot(
            self._dependencies.api, self._workspace_id(receipt, step)
        )
        later = observed.builds[len(step.pre_delete_build_sequence) :]
        if observed.workspace is not None or len(later) != 1 or later[0].status != "deleted":
            block_step(
                self._dependencies, receipt, step, "verified workspace deletion drifted"
            )

    def _workspace_id(self, receipt: MigrationReceipt, step: MigrationStep) -> str:
        if step.workspace_id is None:
            return block_step(self._dependencies, receipt, step, "workspace UUID binding is absent")
        return str(step.workspace_id)
