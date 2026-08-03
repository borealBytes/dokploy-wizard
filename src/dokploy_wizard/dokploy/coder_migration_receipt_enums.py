from __future__ import annotations

from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    ReceiptSchemaError,
    ReceiptStatus,
    StepKind,
    StepStatus,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderBuildStatus, JsonValue


def nullable_build_status(value: JsonValue | None) -> CoderBuildStatus | None:
    match value:
        case None:
            return None
        case "pending" | "starting" | "running" | "stopping" | "stopped" | "failed" as status:
            return status
        case "canceling" | "canceled" | "deleting" | "deleted" as status:
            return status
        case _:
            raise ReceiptSchemaError("latest_build_status is unknown")


def receipt_status(value: JsonValue | None) -> ReceiptStatus:
    match value:
        case "planned" | "running" | "blocked" | "completed" | "failed" as status:
            return status
        case _:
            raise ReceiptSchemaError("receipt status is unknown")


def step_kind(value: JsonValue | None) -> StepKind:
    match value:
        case "inventory" | "rename_template" | "push_template" | "delete_workspace" as kind:
            return kind
        case "delete_template" | "verify" as kind:
            return kind
        case _:
            raise ReceiptSchemaError("step kind is unknown")


def step_status(value: JsonValue | None) -> StepStatus:
    match value:
        case "pending" | "intent" | "submitted" | "verified" | "blocked" | "failed" as status:
            return status
        case _:
            raise ReceiptSchemaError("step status is unknown")
