from __future__ import annotations

from dataclasses import dataclass, replace

from dokploy_wizard.dokploy.coder_migration_mutation_hash import (
    mutation_request_sha256,
    mutation_response_sha256,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderTemplate, JsonValue
from dokploy_wizard.dokploy.coder_template_migration_checkpoint import (
    block_step,
    checkpoint_step,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationDependencies,
    TemplateMigrationTarget,
)


@dataclass(frozen=True, slots=True)
class _PushObservation:
    template: CoderTemplate
    active_version: str
    versions: tuple[str, ...]


class TemplatePushExecutor:
    def __init__(
        self,
        dependencies: TemplateMigrationDependencies,
        targets: tuple[TemplateMigrationTarget, ...],
    ) -> None:
        self._dependencies = dependencies
        self._targets = {target.name: target for target in targets}

    def run(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        target = self._target(receipt, step)
        if step.status == "verified":
            self._observe_intended(receipt, step)
            return receipt
        if step.status == "submitted":
            return self._verify(receipt, step)
        if step.status != "intent":
            return block_step(
                self._dependencies, receipt, step, "push receipt is not recoverable"
            )
        initial = self._initial_observation(receipt, step, target)
        if initial is not None and initial.active_version == target.version_name:
            submitted = self._submitted(step, target, initial)
            receipt = checkpoint_step(self._dependencies, receipt, submitted, "running")
            return self._verify(receipt, submitted)
        self._dependencies.crash_hook.hit("before_request")
        self._dependencies.pusher.push(target)
        self._dependencies.crash_hook.hit("after_response")
        observed = self._observe_intended(receipt, step)
        submitted = self._submitted(step, target, observed)
        receipt = checkpoint_step(self._dependencies, receipt, submitted, "running")
        return self._verify(receipt, submitted)

    def _verify(self, receipt: MigrationReceipt, step: MigrationStep) -> MigrationReceipt:
        observed = self._observe_intended(receipt, step)
        target = self._target(receipt, step)
        submitted = self._submitted(step, target, observed)
        verified = replace(
            submitted,
            status="verified",
            post_inventory_sha256=step.pre_inventory_sha256,
            updated_at=self._dependencies.clock(),
        )
        return checkpoint_step(self._dependencies, receipt, verified, "running")

    def _initial_observation(
        self,
        receipt: MigrationReceipt,
        step: MigrationStep,
        target: TemplateMigrationTarget,
    ) -> _PushObservation | None:
        templates = self._dependencies.api.list_templates(str(step.organization_id))
        matches = tuple(template for template in templates if template.name == step.name)
        active = self._dependencies.pusher.active_version_name(step.name or "")
        versions = self._dependencies.pusher.version_names(step.name or "")
        if len(set(versions)) != len(versions) or (
            active is not None and active not in versions
        ):
            return block_step(
                self._dependencies, receipt, step, "push version inventory is ambiguous"
            )
        if step.template_id is None:
            if not matches and active is None and not versions:
                return None
            if len(matches) == 1:
                return self._require_intended(receipt, step, matches[0], active, versions)
            return block_step(
                self._dependencies,
                receipt,
                step,
                "absent push recovery requires exactly one intended template",
            )
        if len(matches) != 1 or matches[0].id != step.template_id:
            return block_step(
                self._dependencies, receipt, step, "existing push template UUID drifted"
            )
        if active == step.template_version_name:
            return self._require_intended(receipt, step, matches[0], active, versions)
        if active != step.active_version_name:
            return block_step(
                self._dependencies, receipt, step, "existing push version inventory drifted"
            )
        intended_count = versions.count(target.version_name)
        if intended_count > 1 or (intended_count == 1 and active != target.version_name):
            return block_step(
                self._dependencies, receipt, step, "intended push digest exists but is inactive"
            )
        if active is None:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "existing template has no active version",
            )
        return _PushObservation(matches[0], active, versions)

    def _require_intended(
        self,
        receipt: MigrationReceipt,
        step: MigrationStep,
        template: CoderTemplate,
        active: str | None,
        versions: tuple[str, ...],
    ) -> _PushObservation:
        intended_count = versions.count(step.template_version_name or "")
        if intended_count != 1 or active != step.template_version_name:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "push recovery requires exactly one active intended digest",
            )
        if active is None:
            return block_step(self._dependencies, receipt, step, "push has no active version")
        return _PushObservation(template, active, versions)

    def _observe_intended(
        self, receipt: MigrationReceipt, step: MigrationStep
    ) -> _PushObservation:
        templates = self._dependencies.api.list_templates(str(step.organization_id))
        matches = tuple(template for template in templates if template.name == step.name)
        if len(matches) != 1:
            return block_step(
                self._dependencies,
                receipt,
                step,
                "push recovery requires exactly one template match",
            )
        template = matches[0]
        if step.template_id is not None and template.id != step.template_id:
            return block_step(
                self._dependencies, receipt, step, "push template UUID changed"
            )
        active = self._dependencies.pusher.active_version_name(step.name or "")
        versions = self._dependencies.pusher.version_names(step.name or "")
        return self._require_intended(receipt, step, template, active, versions)

    def _target(
        self, receipt: MigrationReceipt, step: MigrationStep
    ) -> TemplateMigrationTarget:
        target = self._targets.get(step.name or "")
        if target is None:
            return block_step(self._dependencies, receipt, step, "push target is unknown")
        if (
            target.rendered_sha256 != step.rendered_sha256
            or target.runtime_lock_sha256 != step.runtime_lock_sha256
            or target.version_name != step.template_version_name
        ):
            return block_step(
                self._dependencies, receipt, step, "push target digest binding changed"
            )
        return target

    def _submitted(
        self,
        step: MigrationStep,
        target: TemplateMigrationTarget,
        observed: _PushObservation,
    ) -> MigrationStep:
        body: dict[str, JsonValue] = {
            "rendered_sha256": target.rendered_sha256,
            "runtime_lock_sha256": target.runtime_lock_sha256,
            "template_version_name": target.version_name,
        }
        response: dict[str, JsonValue] = {
            "active_version_name": observed.active_version,
            "template_id": observed.template.id,
        }
        path = f"coder-cli://templates/{target.name}/push"
        return replace(
            step,
            status="submitted",
            request_sha256=mutation_request_sha256("POST", path, body),
            response_sha256=mutation_response_sha256("POST", path, body, response),
            updated_at=self._dependencies.clock(),
        )
