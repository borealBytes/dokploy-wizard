"""Coder destroy-mode service transaction orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_destroy_authorization import coder_secret_owner_id
from dokploy_wizard.dokploy.coder_service_teardown import (
    CoderServiceTeardown,
    CoderServiceTeardownBinding,
    CoderServiceTeardownError,
)
from dokploy_wizard.packs.coder import CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.state import DesiredState
from dokploy_wizard.uninstall.contracts import (
    CoderSecretDestroyingBackend,
    UninstallBackend,
)
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan


@dataclass(frozen=True, slots=True)
class CoderServiceDeletion:
    state_dir: Path
    desired_state: DesiredState
    deletion: PlannedDeletion
    backend: UninstallBackend
    mode: str


def delete_coder_service(context: CoderServiceDeletion) -> CoderServiceTeardown:
    if not isinstance(context.backend, CoderSecretDestroyingBackend):
        raise UninstallExecutionError("Coder secret destroy backend is unavailable.")
    hostname = context.desired_state.hostnames.get("coder")
    if hostname is None:
        raise UninstallExecutionError("Coder secret owner hostname is unavailable.")
    transaction = CoderServiceTeardown(
        context.state_dir,
        CoderServiceTeardownBinding(
            stack_name=context.desired_state.stack_name,
            resource_type=context.deletion.resource.resource_type,
            resource_id=context.deletion.resource.resource_id,
            resource_scope=context.deletion.resource.scope,
            owner_id=coder_secret_owner_id(context.desired_state.stack_name, hostname),
        ),
    )
    try:
        receipt = transaction.current()
        if receipt is None:
            context.backend.destroy_coder_secrets(desired_state=context.desired_state)
            receipt = transaction.begin()
        if receipt.phase == "intent":
            context.backend.delete(context.deletion)
            transaction.complete()
        transaction.finalize()
    except CoderServiceTeardownError as error:
        raise UninstallExecutionError("Coder service teardown transaction failed.") from error
    return transaction


def delete_with_coder_lifecycle(
    context: CoderServiceDeletion,
) -> CoderServiceTeardown | None:
    if (
        context.mode == "destroy"
        and context.deletion.resource.resource_type == CODER_SERVICE_RESOURCE_TYPE
    ):
        return delete_coder_service(context)
    context.backend.delete(context.deletion)
    return None


def finish_orphaned_coder_teardown(state_dir: Path) -> None:
    try:
        CoderServiceTeardown.finish_orphaned(state_dir)
    except CoderServiceTeardownError as error:
        raise UninstallExecutionError("Coder service teardown transaction failed.") from error


def finish_orphaned_coder_teardown_for_plan(
    state_dir: Path, plan: UninstallPlan
) -> None:
    has_coder_service_delete = any(
        deletion.resource.resource_type == CODER_SERVICE_RESOURCE_TYPE
        for deletion in plan.deletions
    )
    if plan.mode == "destroy" and not has_coder_service_delete:
        finish_orphaned_coder_teardown(state_dir)


def finish_coder_service(transaction: CoderServiceTeardown) -> None:
    try:
        transaction.finish()
    except CoderServiceTeardownError as error:
        raise UninstallExecutionError("Coder service teardown transaction failed.") from error
