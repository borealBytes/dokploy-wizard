from __future__ import annotations

from dataclasses import replace
from typing import Literal, NoReturn

from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    ReceiptUpdate,
)
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationBlockedError
from dokploy_wizard.dokploy.coder_template_migration_models import TemplateMigrationDependencies


def checkpoint_step(
    dependencies: TemplateMigrationDependencies,
    receipt: MigrationReceipt,
    step: MigrationStep,
    status: Literal["running", "completed"],
    post_inventory_sha256: str | None = None,
) -> MigrationReceipt:
    dependencies.crash_hook.hit("before_journal")
    dependencies.crash_hook.hit("before_checkpoint")
    steps = tuple(
        step if candidate.step_id == step.step_id else candidate for candidate in receipt.steps
    )
    return dependencies.receipt_store.checkpoint(
        receipt,
        ReceiptUpdate(
            status=status,
            updated_at=dependencies.clock(),
            steps=steps,
            post_inventory_sha256=post_inventory_sha256,
        ),
    )


def block_step(
    dependencies: TemplateMigrationDependencies,
    receipt: MigrationReceipt,
    step: MigrationStep,
    reason: str,
) -> NoReturn:
    blocked = replace(step, status="blocked", error=reason, updated_at=dependencies.clock())
    steps = tuple(
        blocked if candidate.step_id == step.step_id else candidate for candidate in receipt.steps
    )
    dependencies.crash_hook.hit("before_journal")
    dependencies.crash_hook.hit("before_checkpoint")
    dependencies.receipt_store.checkpoint(
        receipt,
        ReceiptUpdate(
            status="blocked",
            updated_at=dependencies.clock(),
            steps=steps,
            post_inventory_sha256=None,
        ),
    )
    raise CoderMigrationBlockedError(reason)


def current_step(receipt: MigrationReceipt, step_id: str) -> MigrationStep:
    matches = tuple(step for step in receipt.steps if step.step_id == step_id)
    if len(matches) != 1:
        raise CoderMigrationBlockedError("migration receipt step identity is ambiguous")
    return matches[0]
