from __future__ import annotations

from datetime import datetime
from typing import Final, assert_never

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptError,
    CoderSecretReceiptStep,
    ReceiptStatus,
    SecretOperation,
    StepStatus,
    metadata_sha256,
)
from dokploy_wizard.state.sync_schema import JsonValue

_EMPTY_METADATA_SHA256: Final = metadata_sha256({"exists": False})
_RECEIPT_KEYS: Final = frozenset(("schema_version", "owner_id", "status", "steps"))
_STEP_KEYS: Final = frozenset(
    (
        "secret_name",
        "secret_id",
        "env_name",
        "description",
        "operation",
        "status",
        "pre_metadata_sha256",
        "second_pre_metadata_sha256",
        "source_value_sha256",
        "expected_post_sha256",
        "response_sha256",
        "workspace_verification_sha256",
        "updated_at",
    )
)


def parse_receipt(value: JsonValue) -> CoderSecretReceipt:
    mapping = _mapping(value, _RECEIPT_KEYS, "receipt")
    if mapping["schema_version"] != 1 or isinstance(mapping["schema_version"], bool):
        raise CoderSecretReceiptError("Coder secret receipt version is unsupported")
    status = _receipt_status(mapping["status"])
    owner_id = _hash(mapping["owner_id"], "owner_id")
    steps_value = mapping["steps"]
    if not isinstance(steps_value, list):
        raise CoderSecretReceiptError("Coder secret receipt steps are invalid")
    steps = tuple(_step(item) for item in steps_value)
    names = tuple(step.secret_name for step in steps)
    if tuple(sorted(names)) != names:
        raise CoderSecretReceiptError("Coder secret receipt steps are not sorted")
    if len(set(names)) != len(names):
        raise CoderSecretReceiptError("Coder secret receipt steps are duplicated")
    _validate_receipt_lifecycle(status, steps)
    return CoderSecretReceipt(owner_id=owner_id, status=status, steps=steps)


def _step(value: JsonValue) -> CoderSecretReceiptStep:
    mapping = _mapping(value, _STEP_KEYS, "receipt step")
    step = CoderSecretReceiptStep(
        secret_name=_text(mapping["secret_name"], "secret_name"),
        secret_id=_nullable_text(mapping["secret_id"], "secret_id"),
        env_name=_text(mapping["env_name"], "env_name"),
        description=_text(mapping["description"], "description"),
        operation=_operation(mapping["operation"]),
        status=_step_status(mapping["status"]),
        pre_metadata_sha256=_nullable_hash(
            mapping["pre_metadata_sha256"], "pre_metadata_sha256"
        ),
        second_pre_metadata_sha256=_nullable_hash(
            mapping["second_pre_metadata_sha256"], "second_pre_metadata_sha256"
        ),
        source_value_sha256=_hash(mapping["source_value_sha256"], "source_value_sha256"),
        expected_post_sha256=_hash(mapping["expected_post_sha256"], "expected_post_sha256"),
        response_sha256=_nullable_hash(mapping["response_sha256"], "response_sha256"),
        workspace_verification_sha256=_nullable_hash(
            mapping["workspace_verification_sha256"], "workspace_verification_sha256"
        ),
        updated_at=_timestamp(mapping["updated_at"]),
    )
    _validate_step_lifecycle(step)
    return step


def _validate_receipt_lifecycle(
    status: ReceiptStatus, steps: tuple[CoderSecretReceiptStep, ...]
) -> None:
    match status:
        case "planned":
            _require(not steps, "planned receipt has steps")
        case "running":
            _require(bool(steps), "running receipt has no steps")
            _require(all(step.status != "blocked" for step in steps), "running receipt is blocked")
        case "blocked":
            _require(bool(steps), "blocked receipt has no steps")
            _require(
                any(step.status == "blocked" for step in steps),
                "blocked receipt has no block",
            )
        case "completed":
            _require(bool(steps), "completed receipt has no steps")
            _require(
                all(step.status == "verified" for step in steps),
                "completed receipt has unresolved step",
            )
        case "failed":
            _require(bool(steps), "failed receipt has no steps")
        case _ as unreachable_receipt_status:
            assert_never(unreachable_receipt_status)


def _validate_step_lifecycle(step: CoderSecretReceiptStep) -> None:
    _require(step.pre_metadata_sha256 is not None, "receipt step pre metadata is absent")
    _require(step.second_pre_metadata_sha256 is not None, "receipt step second metadata is absent")
    if step.status != "blocked":
        _require(
            step.pre_metadata_sha256 == step.second_pre_metadata_sha256,
            "receipt step pre metadata differs",
        )
    match step.status:
        case "intent":
            _require(step.response_sha256 is None, "receipt intent has a response")
            _require(step.workspace_verification_sha256 is None, "receipt intent is verified")
        case "submitted":
            _require(step.response_sha256 is not None, "receipt submission lacks a response")
            _require(step.workspace_verification_sha256 is None, "receipt submission is verified")
        case "verified":
            _require(step.response_sha256 is not None, "receipt verification lacks a response")
            _require(
                step.workspace_verification_sha256 is not None,
                "receipt verification lacks workspace evidence",
            )
        case "blocked":
            _require(
                step.workspace_verification_sha256 is None,
                "blocked receipt has workspace evidence",
            )
        case _ as unreachable_step_status:
            assert_never(unreachable_step_status)
    match step.operation:
        case "create":
            _require(
                step.pre_metadata_sha256 == _EMPTY_METADATA_SHA256,
                "create receipt pre metadata is invalid",
            )
            _require(
                step.expected_post_sha256 == _expected_hash(None, step),
                "create receipt expected metadata is invalid",
            )
            if step.status == "verified":
                _require(step.secret_id is not None, "verified create lacks secret id")
        case "update" | "noop":
            _require(step.secret_id is not None, "existing secret receipt lacks secret id")
            _require(
                step.expected_post_sha256 == _expected_hash(step.secret_id, step),
                "existing receipt expected metadata is invalid",
            )
            if step.status != "blocked":
                _require(
                    step.pre_metadata_sha256 == step.expected_post_sha256,
                    "existing receipt pre metadata is invalid",
                )
        case _ as unreachable_operation:
            assert_never(unreachable_operation)


def _expected_hash(secret_id: str | None, step: CoderSecretReceiptStep) -> str:
    return metadata_sha256(
        {
            "secret_id": secret_id,
            "name": step.secret_name,
            "env_name": step.env_name,
            "description": step.description,
        }
    )


def _mapping(value: JsonValue, keys: frozenset[str], label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise CoderSecretReceiptError(f"Coder secret {label} has unknown or missing fields")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CoderSecretReceiptError(f"Coder secret {label} is invalid")
    return value


def _nullable_text(value: JsonValue, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _hash(value: JsonValue, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CoderSecretReceiptError(f"Coder secret {label} is invalid")
    return text


def _nullable_hash(value: JsonValue, label: str) -> str | None:
    return None if value is None else _hash(value, label)


def _timestamp(value: JsonValue) -> str:
    text = _text(value, "updated_at")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise CoderSecretReceiptError("Coder secret updated_at is invalid") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise CoderSecretReceiptError("Coder secret updated_at is invalid")
    return text


def _receipt_status(value: JsonValue) -> ReceiptStatus:
    text = _text(value, "status")
    match text:
        case "planned" | "running" | "blocked" | "completed" | "failed":
            return text
        case _:
            raise CoderSecretReceiptError("Coder secret receipt status is invalid")


def _operation(value: JsonValue) -> SecretOperation:
    text = _text(value, "operation")
    match text:
        case "create" | "update" | "noop":
            return text
        case _:
            raise CoderSecretReceiptError("Coder secret operation is invalid")


def _step_status(value: JsonValue) -> StepStatus:
    text = _text(value, "step status")
    match text:
        case "intent" | "submitted" | "verified" | "blocked":
            return text
        case _:
            raise CoderSecretReceiptError("Coder secret step status is invalid")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise CoderSecretReceiptError(f"Coder secret receipt {reason}")
