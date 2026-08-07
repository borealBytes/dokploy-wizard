from __future__ import annotations

from hashlib import sha256

import pytest

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptError,
    CoderSecretReceiptStep,
    StepStatus,
    metadata_sha256,
    parse_receipt,
)
from dokploy_wizard.state.sync_schema import JsonValue


def _step(name: str, *, status: StepStatus = "verified") -> CoderSecretReceiptStep:
    source_value_sha256 = sha256(name.encode()).hexdigest()
    return CoderSecretReceiptStep(
        secret_name=name,
        secret_id="owned-secret",
        env_name="TEST_ENV",
        description="test secret metadata",
        operation="update",
        status=status,
        pre_metadata_sha256=metadata_sha256(
            {
                "secret_id": "owned-secret",
                "name": name,
                "env_name": "TEST_ENV",
                "description": "test secret metadata",
            }
        ),
        second_pre_metadata_sha256=metadata_sha256(
            {
                "secret_id": "owned-secret",
                "name": name,
                "env_name": "TEST_ENV",
                "description": "test secret metadata",
            }
        ),
        source_value_sha256=source_value_sha256,
        expected_post_sha256=metadata_sha256(
            {
                "secret_id": "owned-secret",
                "name": name,
                "env_name": "TEST_ENV",
                "description": "test secret metadata",
            }
        ),
        response_sha256="a" * 64,
        workspace_verification_sha256="b" * 64,
        updated_at="2026-07-30T00:00:00Z",
    )


def _receipt_payload() -> dict[str, JsonValue]:
    receipt = CoderSecretReceipt(
        owner_id="a" * 64,
        status="completed",
        steps=(_step("test-secret"),),
    )
    return receipt.to_dict()


def _first_step(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    steps = payload["steps"]
    assert isinstance(steps, list)
    first = steps[0]
    assert isinstance(first, dict)
    return first


@pytest.mark.parametrize("tamper", ("version", "enum", "field", "order", "hash"))
def test_receipt_rejects_unknown_version_enum_field_order_and_hash_states(tamper: str) -> None:
    payload = _receipt_payload()

    match tamper:
        case "version":
            payload["schema_version"] = 2
        case "enum":
            payload["status"] = "unknown"
        case "field":
            del payload["owner_id"]
        case "order":
            payload["steps"] = [_step("z-secret").to_dict(), _step("a-secret").to_dict()]
        case "hash":
            _first_step(payload)["response_sha256"] = "not-a-hash"
        case _:
            raise AssertionError("test case is invalid")

    with pytest.raises(CoderSecretReceiptError):
        parse_receipt(payload)


def test_receipt_rejects_verified_step_without_a_durable_submitted_hash() -> None:
    payload = _receipt_payload()
    _first_step(payload)["response_sha256"] = None

    with pytest.raises(CoderSecretReceiptError):
        parse_receipt(payload)


def test_receipt_version_failure_has_typed_schema_origin() -> None:
    payload = _receipt_payload()
    payload["schema_version"] = 2

    with pytest.raises(CoderSecretReceiptError) as raised:
        parse_receipt(payload)

    assert raised.value.kind == "schema_version"


def test_receipt_rejects_completed_receipt_with_an_unresolved_step() -> None:
    payload = _receipt_payload()
    unresolved = _step("test-secret", status="intent").to_dict()
    unresolved["response_sha256"] = None
    unresolved["workspace_verification_sha256"] = None
    payload["steps"] = [unresolved]

    with pytest.raises(CoderSecretReceiptError):
        parse_receipt(payload)
