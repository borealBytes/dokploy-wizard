from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Literal, NoReturn, assert_never

from dokploy_wizard.dokploy.coder_migration_inventory import (
    TemplateDeletionSnapshot,
    template_deletion_snapshot,
)
from dokploy_wizard.dokploy.coder_migration_mutation_hash import (
    mutation_request_sha256,
    mutation_response_sha256,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    ReceiptUpdate,
)
from dokploy_wizard.dokploy.coder_migration_template_models import TemplateDeletionDependencies
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationBlockedError


def recover_template_delete(dependencies: TemplateDeletionDependencies) -> MigrationReceipt:
    receipt = dependencies.receipt_store.load()
    if receipt is None or len(receipt.steps) != 1:
        raise CoderMigrationBlockedError("migration receipt is absent or has an unknown step count")
    step = receipt.steps[0]
    if step.kind != "delete_template" or step.template_id is None or step.organization_id is None:
        raise CoderMigrationBlockedError("migration receipt is not a template deletion intent")
    match step.status:
        case "intent":
            submitted = _recovery_submitted_step(dependencies, step)
            receipt = checkpoint_template_delete(dependencies, receipt, "running", submitted, None)
            return _complete_template_delete(dependencies, receipt, submitted)
        case "submitted":
            return _complete_template_delete(dependencies, receipt, step)
        case "verified":
            _require_absence(dependencies, receipt, step)
            return receipt
        case "blocked" | "failed" | "pending":
            raise CoderMigrationBlockedError("template deletion receipt is terminal or invalid")
        case unreachable:
            assert_never(unreachable)


def complete_template_delete(
    dependencies: TemplateDeletionDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> MigrationReceipt:
    return _complete_template_delete(dependencies, receipt, step)


def checkpoint_template_delete(
    dependencies: TemplateDeletionDependencies,
    receipt: MigrationReceipt,
    status: Literal["running", "blocked", "completed", "failed"],
    step: MigrationStep,
    post_inventory_sha256: str | None,
) -> MigrationReceipt:
    dependencies.crash_hook.hit("before_journal")
    dependencies.crash_hook.hit("before_checkpoint")
    return dependencies.receipt_store.checkpoint(
        receipt,
        ReceiptUpdate(
            status=status,
            updated_at=dependencies.clock(),
            steps=(step,),
            post_inventory_sha256=post_inventory_sha256,
        ),
    )


def block_template_delete(
    dependencies: TemplateDeletionDependencies,
    receipt: MigrationReceipt,
    step: MigrationStep,
    reason: str,
    observed: TemplateDeletionSnapshot,
) -> NoReturn:
    blocked = replace(step, status="blocked", error=reason, updated_at=dependencies.clock())
    checkpoint_template_delete(
        dependencies, receipt, "blocked", blocked, observed.inventory_sha256
    )
    raise CoderMigrationBlockedError(reason)


def template_delete_request_hash(template_id: str) -> str:
    return mutation_request_sha256("DELETE", f"/api/v2/templates/{template_id}", None)


def template_delete_response_hash(template_id: str, response: bytes) -> str:
    return mutation_response_sha256(
        "DELETE",
        f"/api/v2/templates/{template_id}",
        None,
        {"body_sha256": hashlib.sha256(response).hexdigest()},
    )


def _complete_template_delete(
    dependencies: TemplateDeletionDependencies,
    receipt: MigrationReceipt,
    step: MigrationStep,
) -> MigrationReceipt:
    dependencies.crash_hook.hit("before_poll")
    observed = _snapshot(dependencies, step)
    dependencies.crash_hook.hit("after_poll")
    if observed.template is not None or observed.dependent_workspace_ids:
        block_template_delete(
            dependencies,
            receipt,
            step,
            "template deletion lacks complete absence proof",
            observed,
        )
    verified = replace(
        step,
        status="verified",
        post_inventory_sha256=observed.inventory_sha256,
        updated_at=dependencies.clock(),
    )
    dependencies.crash_hook.hit("before_verified_checkpoint")
    return checkpoint_template_delete(
        dependencies, receipt, "completed", verified, observed.inventory_sha256
    )


def _require_absence(
    dependencies: TemplateDeletionDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> None:
    observed = _snapshot(dependencies, step)
    if observed.template is not None or observed.dependent_workspace_ids:
        block_template_delete(
            dependencies,
            receipt,
            step,
            "verified template deletion no longer has complete absence proof",
            observed,
        )


def _snapshot(
    dependencies: TemplateDeletionDependencies, step: MigrationStep
) -> TemplateDeletionSnapshot:
    if step.organization_id is None or step.template_id is None:
        raise CoderMigrationBlockedError("template deletion receipt lacks UUID binding")
    return template_deletion_snapshot(dependencies.api, step.organization_id, step.template_id)


def _recovery_submitted_step(
    dependencies: TemplateDeletionDependencies, step: MigrationStep
) -> MigrationStep:
    if step.template_id is None:
        raise CoderMigrationBlockedError("template deletion receipt lacks template UUID")
    return replace(
        step,
        status="submitted",
        request_sha256=template_delete_request_hash(step.template_id),
        response_sha256=mutation_response_sha256(
            "DELETE",
            f"/api/v2/templates/{step.template_id}",
            None,
            {"recovery": "exact-absence"},
        ),
        updated_at=dependencies.clock(),
    )
