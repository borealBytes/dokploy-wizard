from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from dokploy_wizard.dokploy.coder_migration_receipt_enums import (
    nullable_build_status,
    receipt_status,
    step_kind,
    step_status,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    ReceiptSchemaError,
)
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderId,
    JsonValue,
    parse_build,
    parse_coder_id,
)

_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "operation_id",
        "generation",
        "cas_token",
        "status",
        "created_at",
        "updated_at",
        "desired_fingerprint",
        "pre_inventory_sha256",
        "post_inventory_sha256",
        "steps",
    }
)
_STEP_KEYS: Final = frozenset(
    {
        "step_id",
        "kind",
        "status",
        "template_id",
        "organization_id",
        "workspace_id",
        "name",
        "latest_build_id",
        "latest_build_number",
        "latest_build_status",
        "pre_delete_build_sequence",
        "stopped",
        "dependent_workspace_ids",
        "dependent_inventory_sha256",
        "desired_fingerprint",
        "pre_inventory_sha256",
        "post_inventory_sha256",
        "rendered_sha256",
        "runtime_lock_sha256",
        "template_version_name",
        "active_version_name",
        "request_sha256",
        "response_sha256",
        "submitted_delete_build_id",
        "submitted_delete_build_number",
        "error",
        "created_at",
        "updated_at",
    }
)


def parse_receipt_value(value: JsonValue) -> MigrationReceipt:
    mapping = _exact_mapping(value, _RECEIPT_KEYS, "receipt")
    _equal(mapping.get("schema_version"), 1, "schema_version")
    steps = _array(mapping.get("steps"), "steps")
    if not steps:
        raise ReceiptSchemaError("steps must not be empty")
    receipt = MigrationReceipt(
        operation_id=_operation_id(_text(mapping.get("operation_id"), "operation_id")),
        generation=_integer(mapping.get("generation"), "generation"),
        cas_token=_text(mapping.get("cas_token"), "cas_token"),
        status=receipt_status(mapping.get("status")),
        created_at=_timestamp(_text(mapping.get("created_at"), "created_at"), "created_at"),
        updated_at=_timestamp(_text(mapping.get("updated_at"), "updated_at"), "updated_at"),
        desired_fingerprint=_hash(mapping.get("desired_fingerprint"), "desired_fingerprint"),
        pre_inventory_sha256=_hash(mapping.get("pre_inventory_sha256"), "pre_inventory_sha256"),
        post_inventory_sha256=_nullable_hash(
            mapping.get("post_inventory_sha256"), "post_inventory_sha256"
        ),
        steps=tuple(_step(item) for item in steps),
    )
    if receipt.generation < 0:
        raise ReceiptSchemaError("generation must be non-negative")
    return receipt


def _step(value: JsonValue) -> MigrationStep:
    mapping = _exact_mapping(value, _STEP_KEYS, "step")
    sequence = tuple(
        parse_build(item)
        for item in _array(mapping.get("pre_delete_build_sequence"), "pre_delete_build_sequence")
    )
    for build in sequence:
        _timestamp(build.created_at, "pre_delete_build_sequence.created_at")
    return MigrationStep(
        step_id=_text(mapping.get("step_id"), "step_id"),
        kind=step_kind(mapping.get("kind")),
        status=step_status(mapping.get("status")),
        template_id=_nullable_id(mapping.get("template_id"), "template_id"),
        organization_id=_nullable_id(mapping.get("organization_id"), "organization_id"),
        workspace_id=_nullable_id(mapping.get("workspace_id"), "workspace_id"),
        name=_nullable_text(mapping.get("name"), "name"),
        latest_build_id=_nullable_id(mapping.get("latest_build_id"), "latest_build_id"),
        latest_build_number=_nullable_integer(
            mapping.get("latest_build_number"), "latest_build_number"
        ),
        latest_build_status=nullable_build_status(mapping.get("latest_build_status")),
        pre_delete_build_sequence=sequence,
        stopped=_nullable_bool(mapping.get("stopped"), "stopped"),
        dependent_workspace_ids=_nullable_ids(
            mapping.get("dependent_workspace_ids"), "dependent_workspace_ids"
        ),
        dependent_inventory_sha256=_nullable_hash(
            mapping.get("dependent_inventory_sha256"), "dependent_inventory_sha256"
        ),
        desired_fingerprint=_nullable_hash(
            mapping.get("desired_fingerprint"), "desired_fingerprint"
        ),
        pre_inventory_sha256=_nullable_hash(
            mapping.get("pre_inventory_sha256"), "pre_inventory_sha256"
        ),
        post_inventory_sha256=_nullable_hash(
            mapping.get("post_inventory_sha256"), "post_inventory_sha256"
        ),
        rendered_sha256=_nullable_hash(mapping.get("rendered_sha256"), "rendered_sha256"),
        runtime_lock_sha256=_nullable_hash(
            mapping.get("runtime_lock_sha256"), "runtime_lock_sha256"
        ),
        template_version_name=_nullable_text(
            mapping.get("template_version_name"), "template_version_name"
        ),
        active_version_name=_nullable_text(
            mapping.get("active_version_name"), "active_version_name"
        ),
        request_sha256=_nullable_hash(mapping.get("request_sha256"), "request_sha256"),
        response_sha256=_nullable_hash(mapping.get("response_sha256"), "response_sha256"),
        submitted_delete_build_id=_nullable_id(
            mapping.get("submitted_delete_build_id"), "submitted_delete_build_id"
        ),
        submitted_delete_build_number=_nullable_integer(
            mapping.get("submitted_delete_build_number"), "submitted_delete_build_number"
        ),
        error=_nullable_text(mapping.get("error"), "error"),
        created_at=_timestamp(_text(mapping.get("created_at"), "created_at"), "created_at"),
        updated_at=_timestamp(_text(mapping.get("updated_at"), "updated_at"), "updated_at"),
    )


def _exact_mapping(value: JsonValue, keys: frozenset[str], label: str) -> dict[str, JsonValue]:
    match value:
        case dict() as mapping if frozenset(mapping) == keys:
            return mapping
        case _:
            raise ReceiptSchemaError(f"{label} has an unknown or missing field")


def _array(value: JsonValue | None, label: str) -> list[JsonValue]:
    match value:
        case list() as values:
            return values
        case _:
            raise ReceiptSchemaError(f"{label} must be an array")


def _text(value: JsonValue | None, label: str) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise ReceiptSchemaError(f"{label} must be a non-empty string")


def _nullable_text(value: JsonValue | None, label: str) -> str | None:
    match value:
        case None:
            return None
        case str() as text:
            return text
        case _:
            raise ReceiptSchemaError(f"{label} must be a string or null")


def _integer(value: JsonValue | None, label: str) -> int:
    match value:
        case bool():
            raise ReceiptSchemaError(f"{label} must be an integer")
        case int() as number:
            return number
        case _:
            raise ReceiptSchemaError(f"{label} must be an integer")


def _nullable_integer(value: JsonValue | None, label: str) -> int | None:
    match value:
        case None:
            return None
        case bool():
            raise ReceiptSchemaError(f"{label} must be an integer or null")
        case int() as number:
            return number
        case _:
            raise ReceiptSchemaError(f"{label} must be an integer or null")


def _nullable_bool(value: JsonValue | None, label: str) -> bool | None:
    match value:
        case None:
            return None
        case bool() as flag:
            return flag
        case _:
            raise ReceiptSchemaError(f"{label} must be a boolean or null")


def _nullable_id(value: JsonValue | None, label: str) -> CoderId | None:
    match value:
        case None:
            return None
        case _:
            return parse_coder_id(value, label)


def _nullable_ids(value: JsonValue | None, label: str) -> tuple[CoderId, ...] | None:
    match value:
        case None:
            return None
        case list() as values:
            return tuple(parse_coder_id(item, label) for item in values)
        case _:
            raise ReceiptSchemaError(f"{label} must be an array or null")


def _operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ReceiptSchemaError("operation_id must be a UUID") from error
    if str(parsed) != value:
        raise ReceiptSchemaError("operation_id must use canonical lowercase UUID form")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ReceiptSchemaError(f"{label} must be a canonical UTC timestamp") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ReceiptSchemaError(f"{label} must be a canonical UTC timestamp")
    return value


def _hash(value: JsonValue | None, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ReceiptSchemaError(f"{label} must be a lowercase SHA-256")
    return text


def _nullable_hash(value: JsonValue | None, label: str) -> str | None:
    match value:
        case None:
            return None
        case _:
            return _hash(value, label)


def _equal(value: JsonValue | None, expected: int, label: str) -> None:
    if value != expected or isinstance(value, bool):
        raise ReceiptSchemaError(f"{label} is invalid")
