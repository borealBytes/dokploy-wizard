"""Canonical schema encoding and strict parsing for OpenCode Go cutover receipts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverImage,
    CutoverReceipt,
    CutoverRollback,
    CutoverRow,
    CutoverRowOperation,
    CutoverRowStatus,
    CutoverStatus,
    CutoverVerification,
    OpenCodeGoCutoverError,
    RollbackStatus,
)

_STATUS_VALUES: Final[Mapping[str, CutoverStatus]] = MappingProxyType(
    {
        "intent": "intent",
        "transitional_deployed": "transitional_deployed",
        "rows_reconciled": "rows_reconciled",
        "visibility_verified": "visibility_verified",
        "ready_to_cutover": "ready_to_cutover",
        "dynamic_deployed": "dynamic_deployed",
        "complete": "complete",
        "rollback_blocked": "rollback_blocked",
        "failed": "failed",
    }
)
_ROW_OPERATION_VALUES: Final[Mapping[str, CutoverRowOperation]] = MappingProxyType(
    {"noop": "noop", "create": "create", "update": "update", "delete": "delete"}
)
_ROW_STATUS_VALUES: Final[Mapping[str, CutoverRowStatus]] = MappingProxyType(
    {"intent": "intent", "submitted": "submitted", "verified": "verified", "blocked": "blocked"}
)
_ROLLBACK_STATUS_VALUES: Final[Mapping[str, RollbackStatus]] = MappingProxyType(
    {"not_needed": "not_needed", "restored": "restored", "blocked": "blocked"}
)


def canonical_receipt_bytes(receipt: CutoverReceipt) -> bytes:
    payload = json.dumps(receipt_to_dict(receipt), sort_keys=True, separators=(",", ":"))
    return (payload + "\n").encode()


def receipt_to_dict(receipt: CutoverReceipt) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "operation_id": receipt.operation_id,
        "owner_id": receipt.owner_id,
        "catalog_id": receipt.catalog_id,
        "status": receipt.status,
        "compose_id": receipt.compose_id,
        "pre_image": _image_to_dict(receipt.pre_image),
        "transitional_image": _image_to_dict(receipt.transitional_image),
        "dynamic_image": _image_to_dict(receipt.dynamic_image),
        "rows": [_row_to_dict(row) for row in receipt.rows],
        "verification": {
            "transitional_verified": receipt.verification.transitional_verified,
            "visibility_verified": receipt.verification.visibility_verified,
            "dynamic_verified": receipt.verification.dynamic_verified,
            "aliases_sha256": receipt.verification.aliases_sha256,
        },
        "rollback": {
            "status": receipt.rollback.status,
            "pre_fingerprint": receipt.rollback.pre_fingerprint,
            "post_fingerprint": receipt.rollback.post_fingerprint,
            "current_fingerprint": receipt.rollback.current_fingerprint,
        },
        "updated_at": receipt.updated_at,
    }


def parse_receipt(raw: bytes) -> CutoverReceipt:
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError as error:
        raise OpenCodeGoCutoverError("Cutover receipt is malformed") from error
    payload = _mapping(
        value,
        "receipt",
        {
            "schema_version", "operation_id", "owner_id", "catalog_id", "status",
            "compose_id", "pre_image", "transitional_image", "dynamic_image", "rows",
            "verification", "rollback", "updated_at",
        },
    )
    if payload["schema_version"] != 1:
        raise OpenCodeGoCutoverError("Cutover receipt version is unsupported")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise OpenCodeGoCutoverError("Cutover receipt rows are invalid")
    receipt = CutoverReceipt(
        operation_id=_text(payload["operation_id"], "operation_id"),
        owner_id=_text(payload["owner_id"], "owner_id"),
        catalog_id=_text(payload["catalog_id"], "catalog_id"),
        status=_status(payload["status"]),
        compose_id=_text(payload["compose_id"], "compose_id"),
        pre_image=_image(payload["pre_image"]),
        transitional_image=_image(payload["transitional_image"]),
        dynamic_image=_image(payload["dynamic_image"]),
        rows=tuple(_row(item) for item in rows),
        verification=_verification(payload["verification"]),
        rollback=_rollback(payload["rollback"]),
        updated_at=_text(payload["updated_at"], "updated_at"),
    )
    sorted_names = tuple(sorted(row.model_name for row in receipt.rows))
    if sorted_names != tuple(row.model_name for row in receipt.rows):
        raise OpenCodeGoCutoverError("Cutover receipt rows are not sorted")
    if len({row.model_id for row in receipt.rows}) != len(receipt.rows):
        raise OpenCodeGoCutoverError("Cutover receipt row IDs are not unique")
    if canonical_receipt_bytes(receipt) != raw:
        raise OpenCodeGoCutoverError("Cutover receipt bytes are not canonical")
    return receipt


def _image_to_dict(image: CutoverImage) -> dict[str, JsonValue]:
    return {
        "compose_sha256": image.compose_sha256,
        "config_sha256": image.config_sha256,
        "model_set_sha256": image.model_set_sha256,
    }


def _row_to_dict(row: CutoverRow) -> dict[str, JsonValue]:
    return {
        "model_id": row.model_id,
        "model_name": row.model_name,
        "operation": row.operation,
        "status": row.status,
        "pre_fingerprint": row.pre_fingerprint,
        "intended_fingerprint": row.intended_fingerprint,
        "current_fingerprint": row.current_fingerprint,
        "request_sha256": row.request_sha256,
        "response_sha256": row.response_sha256,
        "bootstrap_static": row.bootstrap_static,
        "verified_at": row.verified_at,
    }


def _image(value: JsonValue) -> CutoverImage:
    payload = _mapping(
        value,
        "image",
        {"compose_sha256", "config_sha256", "model_set_sha256"},
    )
    return CutoverImage(
        _optional_sha(payload["compose_sha256"], "compose_sha256"),
        _optional_sha(payload["config_sha256"], "config_sha256"),
        _optional_sha(payload["model_set_sha256"], "model_set_sha256"),
    )


def _row(value: JsonValue) -> CutoverRow:
    payload = _mapping(
        value,
        "row",
        {
            "model_id", "model_name", "operation", "status", "pre_fingerprint",
            "intended_fingerprint", "current_fingerprint", "request_sha256",
            "response_sha256", "bootstrap_static", "verified_at",
        },
    )
    if not isinstance(payload["bootstrap_static"], bool):
        raise OpenCodeGoCutoverError("Cutover receipt bootstrap_static is invalid")
    return CutoverRow(
        _text(payload["model_id"], "model_id"),
        _text(payload["model_name"], "model_name"),
        _row_operation(payload["operation"]),
        _row_status(payload["status"]),
        _optional_sha(payload["pre_fingerprint"], "pre_fingerprint"),
        _optional_sha(payload["intended_fingerprint"], "intended_fingerprint"),
        _optional_sha(payload["current_fingerprint"], "current_fingerprint"),
        _optional_sha(payload["request_sha256"], "request_sha256"),
        _optional_sha(payload["response_sha256"], "response_sha256"),
        payload["bootstrap_static"],
        _optional_text(payload["verified_at"], "verified_at"),
    )


def _verification(value: JsonValue) -> CutoverVerification:
    payload = _mapping(
        value,
        "verification",
        {"transitional_verified", "visibility_verified", "dynamic_verified", "aliases_sha256"},
    )
    return CutoverVerification(
        _boolean(payload["transitional_verified"], "transitional_verified"),
        _boolean(payload["visibility_verified"], "visibility_verified"),
        _boolean(payload["dynamic_verified"], "dynamic_verified"),
        _optional_sha(payload["aliases_sha256"], "aliases_sha256"),
    )


def _rollback(value: JsonValue) -> CutoverRollback:
    payload = _mapping(
        value,
        "rollback",
        {"status", "pre_fingerprint", "post_fingerprint", "current_fingerprint"},
    )
    return CutoverRollback(
        _rollback_status(payload["status"]),
        _optional_sha(payload["pre_fingerprint"], "pre_fingerprint"),
        _optional_sha(payload["post_fingerprint"], "post_fingerprint"),
        _optional_sha(payload["current_fingerprint"], "current_fingerprint"),
    )


def _mapping(value: JsonValue, label: str, keys: set[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != keys:
        raise OpenCodeGoCutoverError(f"Cutover receipt {label} keys are invalid")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OpenCodeGoCutoverError(f"Cutover receipt {label} is invalid")
    return value


def _optional_text(value: JsonValue, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _optional_sha(value: JsonValue, label: str) -> str | None:
    if value is None:
        return None
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise OpenCodeGoCutoverError(f"Cutover receipt {label} is invalid")
    return text


def _status(value: JsonValue) -> CutoverStatus:
    status = _STATUS_VALUES.get(_text(value, "status"))
    if status is None:
        raise OpenCodeGoCutoverError("Cutover receipt status is invalid")
    return status


def _row_operation(value: JsonValue) -> CutoverRowOperation:
    operation = _ROW_OPERATION_VALUES.get(_text(value, "row operation"))
    if operation is None:
        raise OpenCodeGoCutoverError("Cutover receipt row operation is invalid")
    return operation


def _row_status(value: JsonValue) -> CutoverRowStatus:
    status = _ROW_STATUS_VALUES.get(_text(value, "row status"))
    if status is None:
        raise OpenCodeGoCutoverError("Cutover receipt row status is invalid")
    return status


def _rollback_status(value: JsonValue) -> RollbackStatus:
    status = _ROLLBACK_STATUS_VALUES.get(_text(value, "rollback status"))
    if status is None:
        raise OpenCodeGoCutoverError("Cutover receipt rollback status is invalid")
    return status


def _boolean(value: JsonValue, label: str) -> bool:
    if not isinstance(value, bool):
        raise OpenCodeGoCutoverError(f"Cutover receipt {label} is invalid")
    return value
