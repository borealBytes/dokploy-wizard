from __future__ import annotations

from dataclasses import replace
from typing import Final, Literal, NoReturn, assert_never

from dokploy_wizard.dokploy.coder_migration_inventory import (
    WorkspaceRecoverySnapshot,
    workspace_recovery_snapshot,
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
from dokploy_wizard.dokploy.coder_migration_types import CoderBuild, JsonValue
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
    CoderMigrationDependencies,
)

_DELETE_BODY: Final[dict[str, JsonValue]] = {"orphan": False, "transition": "delete"}


def recover_workspace_delete(dependencies: CoderMigrationDependencies) -> MigrationReceipt:
    receipt = dependencies.receipt_store.load()
    if receipt is None or len(receipt.steps) != 1:
        raise CoderMigrationBlockedError("migration receipt is absent or has an unknown step count")
    step = receipt.steps[0]
    if step.kind != "delete_workspace" or step.workspace_id is None:
        raise CoderMigrationBlockedError("migration receipt is not a workspace deletion intent")
    match step.status:
        case "intent":
            submitted = _submitted_from_recovery(dependencies, receipt, step)
            receipt = _checkpoint(dependencies, receipt, "running", submitted, None)
            return _poll_terminal_delete(dependencies, receipt, submitted)
        case "submitted":
            return _poll_terminal_delete(dependencies, receipt, step)
        case "verified":
            _terminal_proof(dependencies, receipt, step)
            return receipt
        case "blocked" | "failed" | "pending":
            raise CoderMigrationBlockedError("workspace deletion receipt is terminal or invalid")
        case unreachable:
            assert_never(unreachable)


def poll_workspace_delete(
    dependencies: CoderMigrationDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> MigrationReceipt:
    return _poll_terminal_delete(dependencies, receipt, step)


def checkpoint_workspace_delete(
    dependencies: CoderMigrationDependencies,
    receipt: MigrationReceipt,
    status: Literal["running", "blocked", "completed", "failed"],
    step: MigrationStep,
    post_inventory_sha256: str | None,
) -> MigrationReceipt:
    return _checkpoint(dependencies, receipt, status, step, post_inventory_sha256)


def block_workspace_delete(
    dependencies: CoderMigrationDependencies,
    receipt: MigrationReceipt,
    step: MigrationStep,
    reason: str,
    inventory_sha256: str,
) -> NoReturn:
    blocked = replace(step, status="blocked", error=reason, updated_at=dependencies.clock())
    _checkpoint(dependencies, receipt, "blocked", blocked, inventory_sha256)
    raise CoderMigrationBlockedError(reason)


def workspace_delete_request_hash(workspace_id: str) -> str:
    return mutation_request_sha256(
        "POST", f"/api/v2/workspaces/{workspace_id}/builds", _DELETE_BODY
    )


def workspace_delete_response_hash(workspace_id: str, build: CoderBuild) -> str:
    response: dict[str, JsonValue] = {
        "build_number": build.build_number,
        "id": build.id,
        "status": build.status,
        "transition": build.transition,
    }
    return mutation_response_sha256(
        "POST",
        f"/api/v2/workspaces/{workspace_id}/builds",
        _DELETE_BODY,
        response,
    )


def _submitted_from_recovery(
    dependencies: CoderMigrationDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> MigrationStep:
    observed, delete_build = _exact_delete_observation(dependencies, receipt, step)
    if delete_build.status not in {"deleting", "deleted"}:
        block_workspace_delete(
            dependencies,
            receipt,
            step,
            "workspace delete transition is not pending or terminal",
            observed.inventory_sha256,
        )
    return _submitted_step(dependencies, step, delete_build)


def _poll_terminal_delete(
    dependencies: CoderMigrationDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> MigrationReceipt:
    for attempt in range(dependencies.polling.attempts):
        dependencies.crash_hook.hit("before_poll")
        observed, delete_build = _exact_delete_observation(dependencies, receipt, step)
        dependencies.crash_hook.hit("after_poll")
        submitted = _submitted_step(dependencies, step, delete_build)
        if observed.workspace is None and delete_build.status == "deleted":
            verified = replace(
                submitted,
                status="verified",
                post_inventory_sha256=observed.inventory_sha256,
                updated_at=dependencies.clock(),
            )
            dependencies.crash_hook.hit("before_verified_checkpoint")
            return _checkpoint(
                dependencies, receipt, "completed", verified, observed.inventory_sha256
            )
        if observed.workspace is None or delete_build.status != "deleting":
            block_workspace_delete(
                dependencies,
                receipt,
                submitted,
                "workspace delete lacks exact terminal absence proof",
                observed.inventory_sha256,
            )
        if attempt < dependencies.polling.attempts - 1:
            dependencies.polling.sleeper(dependencies.polling.delay_seconds)
    block_workspace_delete(
        dependencies,
        receipt,
        step,
        "workspace delete did not reach terminal state before polling bound",
        observed.inventory_sha256,
    )


def _terminal_proof(
    dependencies: CoderMigrationDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> None:
    observed, delete_build = _exact_delete_observation(dependencies, receipt, step)
    if observed.workspace is not None or delete_build.status != "deleted":
        block_workspace_delete(
            dependencies,
            receipt,
            step,
            "verified workspace deletion no longer has terminal absence proof",
            observed.inventory_sha256,
        )


def _exact_delete_observation(
    dependencies: CoderMigrationDependencies, receipt: MigrationReceipt, step: MigrationStep
) -> tuple[WorkspaceRecoverySnapshot, CoderBuild]:
    if step.workspace_id is None:
        raise CoderMigrationBlockedError("workspace deletion receipt lacks workspace identity")
    observed = workspace_recovery_snapshot(dependencies.api, step.workspace_id)
    if observed.builds[: len(step.pre_delete_build_sequence)] != step.pre_delete_build_sequence:
        block_workspace_delete(
            dependencies,
            receipt,
            step,
            "workspace build history changed",
            observed.inventory_sha256,
        )
    later = observed.builds[len(step.pre_delete_build_sequence) :]
    if len(later) != 1 or later[0].transition != "delete":
        block_workspace_delete(
            dependencies,
            receipt,
            step,
            "workspace delete transition is not exact",
            observed.inventory_sha256,
        )
    delete_build = later[0]
    if (
        step.submitted_delete_build_id is not None
        and (
            delete_build.id != step.submitted_delete_build_id
            or delete_build.build_number != step.submitted_delete_build_number
        )
    ):
        block_workspace_delete(
            dependencies,
            receipt,
            step,
            "workspace delete submission identity changed",
            observed.inventory_sha256,
        )
    return observed, delete_build


def _submitted_step(
    dependencies: CoderMigrationDependencies, step: MigrationStep, build: CoderBuild
) -> MigrationStep:
    if step.workspace_id is None:
        raise CoderMigrationBlockedError("workspace deletion receipt lacks workspace identity")
    return replace(
        step,
        status="submitted",
        request_sha256=workspace_delete_request_hash(step.workspace_id),
        response_sha256=workspace_delete_response_hash(step.workspace_id, build),
        submitted_delete_build_id=build.id,
        submitted_delete_build_number=build.build_number,
        updated_at=dependencies.clock(),
    )


def _checkpoint(
    dependencies: CoderMigrationDependencies,
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
