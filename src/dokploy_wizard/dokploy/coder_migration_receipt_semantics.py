from __future__ import annotations

from typing import Final, TypeVar, assert_never

from dokploy_wizard.dokploy.coder_migration_receipt_bindings import (
    validate_step_binding_unchanged,
)
from dokploy_wizard.dokploy.coder_migration_receipt_rules import (
    validate_receipt_root_state,
    validate_receipt_token,
    validate_root_transition,
    validate_step_transition,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    ReceiptSchemaError,
)

_MAX_STEPS: Final = 64
_MAX_STEP_ID_LENGTH: Final = 160
_MAX_NAME_LENGTH: Final = 128
T = TypeVar("T")


def validate_receipt_semantics(receipt: MigrationReceipt) -> None:
    if len(receipt.steps) > _MAX_STEPS:
        raise ReceiptSchemaError("receipt step count exceeds its bound")
    validate_receipt_token(receipt.cas_token)
    if receipt.updated_at < receipt.created_at:
        raise ReceiptSchemaError("receipt updated_at precedes created_at")
    for step in receipt.steps:
        _validate_step(step)
    validate_receipt_root_state(receipt)


def validate_receipt_transition(current: MigrationReceipt, updated: MigrationReceipt) -> None:
    if (
        updated.operation_id != current.operation_id
        or updated.desired_fingerprint != current.desired_fingerprint
        or updated.pre_inventory_sha256 != current.pre_inventory_sha256
        or updated.created_at != current.created_at
        or updated.generation != current.generation + 1
        or updated.updated_at < current.updated_at
    ):
        raise ReceiptSchemaError("receipt transition changes an immutable binding")
    validate_root_transition(current.status, updated.status)
    if len(updated.steps) != len(current.steps):
        raise ReceiptSchemaError("receipt transition changes the step count")
    for before, after in zip(current.steps, updated.steps, strict=True):
        if before.step_id != after.step_id or before.kind != after.kind:
            raise ReceiptSchemaError("receipt transition changes a step identity")
        validate_step_binding_unchanged(before, after)
        validate_step_transition(before.status, after.status)
    validate_receipt_semantics(updated)


def _validate_step(step: MigrationStep) -> None:
    if not step.step_id or len(step.step_id) > _MAX_STEP_ID_LENGTH:
        raise ReceiptSchemaError("step_id is outside its bound")
    if step.updated_at < step.created_at:
        raise ReceiptSchemaError("step updated_at precedes created_at")
    match step.kind:
        case "inventory" | "verify":
            _validate_observation_step(step)
        case "rename_template":
            _validate_rename_step(step)
        case "push_template":
            _validate_push_step(step)
        case "delete_workspace":
            _validate_workspace_deletion_step(step)
        case "delete_template":
            _validate_template_deletion_step(step)
        case unreachable:
            assert_never(unreachable)


def _validate_observation_step(step: MigrationStep) -> None:
    _require_status(step, "pending", "verified", "blocked", "failed")
    _require_empty_step_identity(step)
    _require_none(step.desired_fingerprint, "observation desired_fingerprint")
    _require_none(step.pre_inventory_sha256, "observation pre_inventory_sha256")
    _require_none(step.request_sha256, "observation request_sha256")
    _require_none(step.response_sha256, "observation response_sha256")
    _require_submission_none(step)
    _require_terminal_fields(step)


def _validate_rename_step(step: MigrationStep) -> None:
    _require_status(step, "intent", "submitted", "verified", "blocked", "failed")
    _require_template_context(step)
    if step.template_id is None or step.organization_id is None:
        raise ReceiptSchemaError("rename template UUID binding is incomplete")
    _require_none(step.workspace_id, "template mutation workspace_id")
    _require_no_build_context(step)
    _require_none(step.dependent_workspace_ids, "template mutation dependent_workspace_ids")
    _require_none(step.dependent_inventory_sha256, "template mutation dependent_inventory_sha256")
    _require_none(step.rendered_sha256, "rename rendered_sha256")
    _require_none(step.runtime_lock_sha256, "rename runtime_lock_sha256")
    _require_none(step.template_version_name, "rename template_version_name")
    _require_none(step.active_version_name, "rename active_version_name")
    _require_mutation_fields(step)


def _validate_push_step(step: MigrationStep) -> None:
    _require_status(step, "intent", "submitted", "verified", "blocked", "failed")
    _require_template_context(step, template_id_required=False)
    if step.organization_id is None:
        raise ReceiptSchemaError("push template organization binding is incomplete")
    if (
        step.rendered_sha256 is None
        or step.runtime_lock_sha256 is None
        or step.template_version_name is None
    ):
        raise ReceiptSchemaError("push template digest binding is incomplete")
    _require_none(step.workspace_id, "template mutation workspace_id")
    _require_no_build_context(step)
    _require_none(step.dependent_workspace_ids, "template mutation dependent_workspace_ids")
    _require_none(step.dependent_inventory_sha256, "template mutation dependent_inventory_sha256")
    _require_mutation_fields(step)


def _validate_workspace_deletion_step(step: MigrationStep) -> None:
    _require_status(step, "intent", "submitted", "verified", "blocked", "failed")
    _require_template_context(step)
    _require_none(step.organization_id, "workspace deletion organization_id")
    if step.workspace_id is None or step.name is None or len(step.name) > _MAX_NAME_LENGTH:
        raise ReceiptSchemaError("workspace deletion identity is incomplete")
    if (
        step.latest_build_id is None
        or step.latest_build_number is None
        or step.latest_build_status != "stopped"
        or step.stopped is not True
    ):
        raise ReceiptSchemaError("workspace deletion stopped/latest tuple is incomplete")
    if not step.pre_delete_build_sequence:
        raise ReceiptSchemaError("workspace deletion pre-delete sequence is empty")
    sequence = step.pre_delete_build_sequence
    if tuple(sorted(sequence, key=lambda build: build.build_number)) != sequence:
        raise ReceiptSchemaError("workspace deletion build sequence is not ordered")
    if len({build.id for build in sequence}) != len(sequence) or len(
        {build.build_number for build in sequence}
    ) != len(sequence):
        raise ReceiptSchemaError("workspace deletion build sequence is not unique")
    latest = sequence[-1]
    if latest.id != step.latest_build_id or latest.build_number != step.latest_build_number:
        raise ReceiptSchemaError("workspace deletion latest tuple is not the sequence tail")
    _require_none(step.dependent_workspace_ids, "workspace deletion dependent_workspace_ids")
    _require_none(step.dependent_inventory_sha256, "workspace deletion dependent_inventory_sha256")
    _require_mutation_fields(step)


def _validate_template_deletion_step(step: MigrationStep) -> None:
    _require_status(step, "intent", "submitted", "verified", "blocked", "failed")
    if (
        step.template_id is None
        or step.organization_id is None
        or step.desired_fingerprint is None
        or step.pre_inventory_sha256 is None
    ):
        raise ReceiptSchemaError("template deletion intent binding is incomplete")
    if step.dependent_workspace_ids != () or step.dependent_inventory_sha256 is None:
        raise ReceiptSchemaError("template deletion dependent proof is incomplete")
    _require_none(step.workspace_id, "template deletion workspace_id")
    _require_none(step.name, "template deletion name")
    _require_no_build_context(step)
    _require_mutation_fields(step)


def _require_template_context(
    step: MigrationStep, *, template_id_required: bool = True
) -> None:
    if (
        (template_id_required and step.template_id is None)
        or step.name is None
        or not step.name
        or len(step.name) > _MAX_NAME_LENGTH
        or step.desired_fingerprint is None
        or step.pre_inventory_sha256 is None
    ):
        raise ReceiptSchemaError("template mutation intent binding is incomplete")


def _require_no_build_context(step: MigrationStep) -> None:
    _require_none(step.latest_build_id, "non-workspace latest_build_id")
    _require_none(step.latest_build_number, "non-workspace latest_build_number")
    _require_none(step.latest_build_status, "non-workspace latest_build_status")
    _require_none(step.stopped, "non-workspace stopped")
    if step.pre_delete_build_sequence:
        raise ReceiptSchemaError("non-workspace pre-delete sequence must be empty")


def _require_empty_step_identity(step: MigrationStep) -> None:
    _require_none(step.template_id, "observation template_id")
    _require_none(step.organization_id, "observation organization_id")
    _require_none(step.workspace_id, "observation workspace_id")
    _require_none(step.name, "observation name")
    _require_no_build_context(step)
    _require_none(step.dependent_workspace_ids, "observation dependent_workspace_ids")
    _require_none(step.dependent_inventory_sha256, "observation dependent_inventory_sha256")


def _require_mutation_fields(step: MigrationStep) -> None:
    match step.status:
        case "intent":
            _require_none(step.request_sha256, "intent request_sha256")
            _require_none(step.response_sha256, "intent response_sha256")
            _require_submission_none(step)
            _require_none(step.post_inventory_sha256, "intent post_inventory_sha256")
            _require_none(step.error, "intent error")
        case "submitted":
            _require_hashes(step)
            _require_submission(step)
            _require_none(step.post_inventory_sha256, "submitted post_inventory_sha256")
            _require_none(step.error, "submitted error")
        case "verified":
            _require_hashes(step)
            _require_submission(step)
            if step.post_inventory_sha256 is None:
                raise ReceiptSchemaError("verified mutation lacks post inventory proof")
            _require_none(step.error, "verified error")
        case "blocked" | "failed":
            if step.error is None or not step.error:
                raise ReceiptSchemaError("terminal mutation error is absent")
        case "pending":
            raise ReceiptSchemaError("mutation step cannot be pending")
        case unreachable:
            assert_never(unreachable)


def _require_terminal_fields(step: MigrationStep) -> None:
    match step.status:
        case "pending":
            _require_none(step.post_inventory_sha256, "pending post_inventory_sha256")
            _require_none(step.error, "pending error")
        case "verified":
            if step.post_inventory_sha256 is None:
                raise ReceiptSchemaError("verified observation lacks post inventory proof")
            _require_none(step.error, "verified observation error")
        case "blocked" | "failed":
            if step.error is None or not step.error:
                raise ReceiptSchemaError("terminal observation error is absent")
        case "intent" | "submitted":
            raise ReceiptSchemaError("observation step has an illegal status")
        case unreachable:
            assert_never(unreachable)


def _require_hashes(step: MigrationStep) -> None:
    if step.request_sha256 is None or step.response_sha256 is None:
        raise ReceiptSchemaError("submitted mutation hashes are incomplete")


def _require_submission(step: MigrationStep) -> None:
    match step.kind:
        case "delete_workspace":
            if step.submitted_delete_build_id is None or step.submitted_delete_build_number is None:
                raise ReceiptSchemaError("workspace delete submission identity is incomplete")
        case "inventory" | "rename_template" | "push_template" | "delete_template" | "verify":
            _require_submission_none(step)
        case unreachable:
            assert_never(unreachable)


def _require_submission_none(step: MigrationStep) -> None:
    _require_none(step.submitted_delete_build_id, "non-submitted delete build ID")
    _require_none(step.submitted_delete_build_number, "non-submitted delete build number")


def _require_status(step: MigrationStep, *allowed: str) -> None:
    if step.status not in allowed:
        raise ReceiptSchemaError("step status is illegal for its kind")


def _require_none(value: T | None, label: str) -> None:
    if value is not None:
        raise ReceiptSchemaError(f"{label} must be null")
