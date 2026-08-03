from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, TypeAlias

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderBuildStatus,
    CoderId,
    JsonValue,
)

ReceiptStatus: TypeAlias = Literal["planned", "running", "blocked", "completed", "failed"]
StepKind: TypeAlias = Literal[
    "inventory", "rename_template", "push_template", "delete_workspace", "delete_template", "verify"
]
StepStatus: TypeAlias = Literal["pending", "intent", "submitted", "verified", "blocked", "failed"]


@dataclass(frozen=True, slots=True)
class MigrationStep:
    step_id: str
    kind: StepKind
    status: StepStatus
    template_id: CoderId | None
    organization_id: CoderId | None
    workspace_id: CoderId | None
    name: str | None
    latest_build_id: CoderId | None
    latest_build_number: int | None
    latest_build_status: CoderBuildStatus | None
    pre_delete_build_sequence: tuple[CoderBuild, ...]
    stopped: bool | None
    dependent_workspace_ids: tuple[CoderId, ...] | None
    dependent_inventory_sha256: str | None
    desired_fingerprint: str | None
    pre_inventory_sha256: str | None
    post_inventory_sha256: str | None
    rendered_sha256: str | None
    runtime_lock_sha256: str | None
    template_version_name: str | None
    active_version_name: str | None
    request_sha256: str | None
    response_sha256: str | None
    submitted_delete_build_id: CoderId | None
    submitted_delete_build_number: int | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    operation_id: str
    generation: int
    cas_token: str
    status: ReceiptStatus
    created_at: str
    updated_at: str
    desired_fingerprint: str
    pre_inventory_sha256: str
    post_inventory_sha256: str | None
    steps: tuple[MigrationStep, ...]


@dataclass(frozen=True, slots=True)
class ReceiptUpdate:
    status: ReceiptStatus
    updated_at: str
    steps: tuple[MigrationStep, ...]
    post_inventory_sha256: str | None


class ReceiptSchemaError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


def new_migration_step(
    *, step_id: str, kind: StepKind, status: StepStatus, created_at: str
) -> MigrationStep:
    return MigrationStep(
        step_id=step_id,
        kind=kind,
        status=status,
        template_id=None,
        organization_id=None,
        workspace_id=None,
        name=None,
        latest_build_id=None,
        latest_build_number=None,
        latest_build_status=None,
        pre_delete_build_sequence=(),
        stopped=None,
        dependent_workspace_ids=None,
        dependent_inventory_sha256=None,
        desired_fingerprint=None,
        pre_inventory_sha256=None,
        post_inventory_sha256=None,
        rendered_sha256=None,
        runtime_lock_sha256=None,
        template_version_name=None,
        active_version_name=None,
        request_sha256=None,
        response_sha256=None,
        submitted_delete_build_id=None,
        submitted_delete_build_number=None,
        error=None,
        created_at=created_at,
        updated_at=created_at,
    )


def new_migration_receipt(
    *,
    operation_id: str,
    desired_fingerprint: str,
    pre_inventory_sha256: str,
    created_at: str,
    step: MigrationStep,
) -> MigrationReceipt:
    return MigrationReceipt(
        operation_id=operation_id,
        generation=0,
        cas_token="",
        status="planned",
        created_at=created_at,
        updated_at=created_at,
        desired_fingerprint=desired_fingerprint,
        pre_inventory_sha256=pre_inventory_sha256,
        post_inventory_sha256=None,
        steps=(step,),
    )


def receipt_bytes(receipt: MigrationReceipt) -> bytes:
    encoded = json.dumps(
        _receipt_value(receipt), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return encoded.encode("utf-8")


def _receipt_value(receipt: MigrationReceipt) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "operation_id": receipt.operation_id,
        "generation": receipt.generation,
        "cas_token": receipt.cas_token,
        "status": receipt.status,
        "created_at": receipt.created_at,
        "updated_at": receipt.updated_at,
        "desired_fingerprint": receipt.desired_fingerprint,
        "pre_inventory_sha256": receipt.pre_inventory_sha256,
        "post_inventory_sha256": receipt.post_inventory_sha256,
        "steps": [_step_value(step) for step in receipt.steps],
    }


def _step_value(step: MigrationStep) -> dict[str, JsonValue]:
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": step.status,
        "template_id": step.template_id,
        "organization_id": step.organization_id,
        "workspace_id": step.workspace_id,
        "name": step.name,
        "latest_build_id": step.latest_build_id,
        "latest_build_number": step.latest_build_number,
        "latest_build_status": step.latest_build_status,
        "pre_delete_build_sequence": [
            _build_value(build) for build in step.pre_delete_build_sequence
        ],
        "stopped": step.stopped,
        "dependent_workspace_ids": None
        if step.dependent_workspace_ids is None
        else list(step.dependent_workspace_ids),
        "dependent_inventory_sha256": step.dependent_inventory_sha256,
        "desired_fingerprint": step.desired_fingerprint,
        "pre_inventory_sha256": step.pre_inventory_sha256,
        "post_inventory_sha256": step.post_inventory_sha256,
        "rendered_sha256": step.rendered_sha256,
        "runtime_lock_sha256": step.runtime_lock_sha256,
        "template_version_name": step.template_version_name,
        "active_version_name": step.active_version_name,
        "request_sha256": step.request_sha256,
        "response_sha256": step.response_sha256,
        "submitted_delete_build_id": step.submitted_delete_build_id,
        "submitted_delete_build_number": step.submitted_delete_build_number,
        "error": step.error,
        "created_at": step.created_at,
        "updated_at": step.updated_at,
    }


def _build_value(build: CoderBuild) -> dict[str, JsonValue]:
    return {
        "id": build.id,
        "build_number": build.build_number,
        "transition": build.transition,
        "status": build.status,
        "created_at": build.created_at,
    }
