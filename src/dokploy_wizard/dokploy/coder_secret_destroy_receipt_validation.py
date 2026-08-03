"""Closed-schema validation for Coder secret destroy journal data."""

from __future__ import annotations

from datetime import datetime
from typing import Final, assert_never
from uuid import UUID

from dokploy_wizard.dokploy.coder_secret_destroy_receipts import (
    CoderSecretDestroyReceipt,
    CoderSecretDestroyReceiptError,
    CoderSecretDestroyStep,
    DestroyReceiptStatus,
    DestroyStepStatus,
)
from dokploy_wizard.dokploy.coder_secret_receipts import metadata_sha256
from dokploy_wizard.state.sync_schema import JsonValue

_EMPTY_METADATA_SHA256: Final = metadata_sha256({"exists": False})
_RECEIPT_KEYS: Final = frozenset(
    (
        "schema_version",
        "owner_id",
        "source_receipt_sha256",
        "status",
        "steps",
    )
)
_STEP_KEYS: Final = frozenset(
    (
        "secret_id",
        "secret_name",
        "env_name",
        "description",
        "status",
        "pre_metadata_sha256",
        "post_metadata_sha256",
        "updated_at",
    )
)


def parse_destroy_receipt(value: JsonValue) -> CoderSecretDestroyReceipt:
    mapping = _mapping(value, _RECEIPT_KEYS, "receipt")
    if mapping["schema_version"] != 1 or isinstance(mapping["schema_version"], bool):
        raise CoderSecretDestroyReceiptError("Coder secret destroy receipt version is unsupported")
    steps_value = mapping["steps"]
    if not isinstance(steps_value, list):
        raise CoderSecretDestroyReceiptError("Coder secret destroy receipt steps are invalid")
    receipt = CoderSecretDestroyReceipt(
        owner_id=_hash(mapping["owner_id"], "owner_id"),
        source_receipt_sha256=_hash(mapping["source_receipt_sha256"], "source receipt"),
        status=_status(mapping["status"]),
        steps=tuple(_step(item) for item in steps_value),
    )
    _validate_receipt(receipt)
    return receipt


def _step(value: JsonValue) -> CoderSecretDestroyStep:
    mapping = _mapping(value, _STEP_KEYS, "receipt step")
    return CoderSecretDestroyStep(
        secret_id=_uuid(mapping["secret_id"]),
        secret_name=_text(mapping["secret_name"], "secret name"),
        env_name=_text(mapping["env_name"], "environment name"),
        description=_text(mapping["description"], "description"),
        status=_step_status(mapping["status"]),
        pre_metadata_sha256=_hash(mapping["pre_metadata_sha256"], "pre metadata"),
        post_metadata_sha256=_nullable_hash(mapping["post_metadata_sha256"], "post metadata"),
        updated_at=_timestamp(mapping["updated_at"]),
    )


def _validate_receipt(receipt: CoderSecretDestroyReceipt) -> None:
    names = tuple(step.secret_name for step in receipt.steps)
    if tuple(sorted(names)) != names or len(set(names)) != len(names):
        raise CoderSecretDestroyReceiptError("Coder secret destroy receipt steps are invalid")
    match receipt.status:
        case "running":
            if not receipt.steps or any(step.status == "blocked" for step in receipt.steps):
                raise CoderSecretDestroyReceiptError("Coder secret destroy receipt is invalid")
        case "completed":
            if not receipt.steps or any(step.status != "deleted" for step in receipt.steps):
                raise CoderSecretDestroyReceiptError("Coder secret destroy receipt is invalid")
        case "blocked":
            if not receipt.steps or not any(step.status == "blocked" for step in receipt.steps):
                raise CoderSecretDestroyReceiptError("Coder secret destroy receipt is invalid")
        case unreachable_receipt_status:
            assert_never(unreachable_receipt_status)
    for step in receipt.steps:
        match step.status:
            case "intent":
                if step.post_metadata_sha256 is not None:
                    raise CoderSecretDestroyReceiptError("Coder secret destroy intent is invalid")
            case "deleted":
                if step.post_metadata_sha256 != _EMPTY_METADATA_SHA256:
                    raise CoderSecretDestroyReceiptError("Coder secret destroy result is invalid")
            case "blocked":
                if step.post_metadata_sha256 is not None:
                    raise CoderSecretDestroyReceiptError("Coder secret destroy block is invalid")
            case unreachable_step_status:
                assert_never(unreachable_step_status)


def _mapping(value: JsonValue, keys: frozenset[str], label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise CoderSecretDestroyReceiptError(
            f"Coder secret destroy {label} has unknown or missing fields"
        )
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CoderSecretDestroyReceiptError(f"Coder secret destroy {label} is invalid")
    return value


def _uuid(value: JsonValue) -> str:
    text = _text(value, "secret id")
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise CoderSecretDestroyReceiptError("Coder secret destroy secret id is invalid") from error
    if str(parsed) != text:
        raise CoderSecretDestroyReceiptError("Coder secret destroy secret id is invalid")
    return text


def _hash(value: JsonValue, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CoderSecretDestroyReceiptError(f"Coder secret destroy {label} is invalid")
    return text


def _nullable_hash(value: JsonValue, label: str) -> str | None:
    return None if value is None else _hash(value, label)


def _status(value: JsonValue) -> DestroyReceiptStatus:
    text = _text(value, "status")
    if text == "running":
        return "running"
    if text == "completed":
        return "completed"
    if text == "blocked":
        return "blocked"
    raise CoderSecretDestroyReceiptError("Coder secret destroy receipt status is invalid")


def _step_status(value: JsonValue) -> DestroyStepStatus:
    text = _text(value, "step status")
    if text == "intent":
        return "intent"
    if text == "deleted":
        return "deleted"
    if text == "blocked":
        return "blocked"
    raise CoderSecretDestroyReceiptError("Coder secret destroy step status is invalid")


def _timestamp(value: JsonValue) -> str:
    text = _text(value, "updated_at")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise CoderSecretDestroyReceiptError(
            "Coder secret destroy updated_at is invalid"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise CoderSecretDestroyReceiptError("Coder secret destroy updated_at is invalid")
    return text
