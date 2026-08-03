from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final, assert_never
from uuid import UUID

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_types import (
    MAX_CREATE_ATTEMPTS,
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
)

_SCHEMA_VERSION: Final = 1
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version", "owner_id", "workspace_id", "workspace_name", "workspace_owner_id",
        "workspace_owner_name", "template_id", "template_name", "env_name",
        "expected_value_sha256", "observed_value_sha256", "create_attempts", "phase",
        "failure_reason", "created_at", "updated_at",
    }
)


def parse_receipt_bytes(payload: bytes) -> WorkspaceVerificationReceipt:
    try:
        value: JsonValue = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _invalid() from error
    match value:
        case dict() as mapping if (
            frozenset(mapping) == _RECEIPT_KEYS
            and mapping.get("schema_version") == _SCHEMA_VERSION
        ):
            receipt = WorkspaceVerificationReceipt(
                owner_id=_text(mapping.get("owner_id")),
                workspace_id=_uuid_or_none(mapping.get("workspace_id")),
                workspace_name=_text(mapping.get("workspace_name")),
                workspace_owner_id=_uuid_or_none(mapping.get("workspace_owner_id")),
                workspace_owner_name=_text_or_none(mapping.get("workspace_owner_name")),
                template_id=_uuid(mapping.get("template_id")),
                template_name=_text(mapping.get("template_name")),
                env_name=_text(mapping.get("env_name")),
                expected_value_sha256=_hash(mapping.get("expected_value_sha256")),
                observed_value_sha256=_hash_or_none(mapping.get("observed_value_sha256")),
                create_attempts=_attempts(mapping.get("create_attempts")),
                phase=_phase(mapping.get("phase")),
                failure_reason=_text_or_none(mapping.get("failure_reason")),
                created_at=_timestamp(mapping.get("created_at")),
                updated_at=_timestamp(mapping.get("updated_at")),
            )
        case _:
            raise _invalid()
    _validate_lifecycle(receipt)
    return receipt


def receipt_bytes(receipt: WorkspaceVerificationReceipt) -> bytes:
    _validate_lifecycle(receipt)
    return json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "owner_id": receipt.owner_id,
            "workspace_id": receipt.workspace_id,
            "workspace_name": receipt.workspace_name,
            "workspace_owner_id": receipt.workspace_owner_id,
            "workspace_owner_name": receipt.workspace_owner_name,
            "template_id": receipt.template_id,
            "template_name": receipt.template_name,
            "env_name": receipt.env_name,
            "expected_value_sha256": receipt.expected_value_sha256,
            "observed_value_sha256": receipt.observed_value_sha256,
            "create_attempts": receipt.create_attempts,
            "phase": receipt.phase.value,
            "failure_reason": receipt.failure_reason,
            "created_at": receipt.created_at,
            "updated_at": receipt.updated_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _validate_lifecycle(receipt: WorkspaceVerificationReceipt) -> None:
    if _parsed_time(receipt.created_at) > _parsed_time(receipt.updated_at):
        raise _invalid()
    match receipt.phase:
        case WorkspaceVerificationPhase.PLANNED:
            _require_unbound(receipt, failure=None)
        case WorkspaceVerificationPhase.CREATED | WorkspaceVerificationPhase.READY:
            _require_bound(receipt, observed=None, failure=None)
        case WorkspaceVerificationPhase.HASHED | WorkspaceVerificationPhase.DELETED:
            _require_bound(receipt, observed=True, failure=None)
        case WorkspaceVerificationPhase.DELETING:
            _require_bound(receipt, observed=False, failure=None)
        case WorkspaceVerificationPhase.BLOCKED:
            _require_blocked(receipt)
        case WorkspaceVerificationPhase.FAILED:
            _require_failed(receipt)
        case unreachable:
            assert_never(unreachable)


def _require_unbound(receipt: WorkspaceVerificationReceipt, *, failure: str | None) -> None:
    if (
        receipt.workspace_id is not None or receipt.workspace_owner_id is not None
        or receipt.workspace_owner_name is not None or receipt.observed_value_sha256 is not None
        or receipt.failure_reason != failure
        or not 0 <= receipt.create_attempts <= MAX_CREATE_ATTEMPTS
    ):
        raise _invalid()


def _require_bound(
    receipt: WorkspaceVerificationReceipt, *, observed: bool | None, failure: str | None
) -> None:
    if (
        receipt.workspace_id is None
        or receipt.workspace_owner_id is None
        or receipt.workspace_owner_name is None
        or receipt.failure_reason != failure
        or not 0 <= receipt.create_attempts <= MAX_CREATE_ATTEMPTS
        or (observed is True and receipt.observed_value_sha256 is None)
        or (observed is None and receipt.observed_value_sha256 is not None)
    ):
        raise _invalid()


def _require_blocked(receipt: WorkspaceVerificationReceipt) -> None:
    if receipt.failure_reason is None:
        raise _invalid()
    if receipt.workspace_id is None:
        _require_unbound(receipt, failure="identity_drift")
    else:
        _require_bound(receipt, observed=False, failure=receipt.failure_reason)


def _require_failed(receipt: WorkspaceVerificationReceipt) -> None:
    if receipt.workspace_id is None:
        _require_unbound(receipt, failure="create_retry_exhausted")
        if receipt.create_attempts != MAX_CREATE_ATTEMPTS:
            raise _invalid()
    elif receipt.failure_reason is None:
        raise _invalid()
    else:
        _require_bound(receipt, observed=False, failure=receipt.failure_reason)


def _phase(value: JsonValue | None) -> WorkspaceVerificationPhase:
    try:
        return WorkspaceVerificationPhase(_text(value))
    except ValueError as error:
        raise _invalid() from error


def _uuid(value: JsonValue | None) -> str:
    text = _text(value)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise _invalid() from error
    if str(parsed) != text:
        raise _invalid()
    return text


def _uuid_or_none(value: JsonValue | None) -> str | None:
    return None if value is None else _uuid(value)


def _hash(value: JsonValue | None) -> str:
    text = _text(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise _invalid()
    return text


def _hash_or_none(value: JsonValue | None) -> str | None:
    return None if value is None else _hash(value)


def _attempts(value: JsonValue | None) -> int:
    if type(value) is int and 0 <= value <= MAX_CREATE_ATTEMPTS:
        return value
    raise _invalid()


def _text(value: JsonValue | None) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise _invalid()


def _text_or_none(value: JsonValue | None) -> str | None:
    return None if value is None else _text(value)


def _timestamp(value: JsonValue | None) -> str:
    text = _text(value)
    _parsed_time(text)
    return text


def _parsed_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise _invalid() from error


def _invalid() -> CoderSecretClientError:
    return CoderSecretClientError("Coder workspace verification receipt is invalid")
