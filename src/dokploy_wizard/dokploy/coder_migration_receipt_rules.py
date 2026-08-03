from __future__ import annotations

from typing import Final, assert_never

from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    ReceiptSchemaError,
    ReceiptStatus,
    StepStatus,
)

_MAX_TOKEN_LENGTH: Final = 128
_TOKEN_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def validate_receipt_root_state(receipt: MigrationReceipt) -> None:
    match receipt.status:
        case "planned":
            if any(step.status not in {"pending", "intent"} for step in receipt.steps):
                raise ReceiptSchemaError("planned receipt has an illegal step status")
            _require_none(receipt.post_inventory_sha256, "planned post_inventory_sha256")
        case "running":
            if any(step.status in {"blocked", "failed"} for step in receipt.steps):
                raise ReceiptSchemaError("running receipt has a terminal error step")
            _require_none(receipt.post_inventory_sha256, "running post_inventory_sha256")
        case "completed":
            if any(step.status != "verified" for step in receipt.steps):
                raise ReceiptSchemaError("completed receipt has a non-verified step")
            if receipt.post_inventory_sha256 is None:
                raise ReceiptSchemaError("completed receipt lacks post inventory proof")
        case "blocked":
            if not any(step.status == "blocked" for step in receipt.steps):
                raise ReceiptSchemaError("blocked receipt lacks a blocked step")
        case "failed":
            if not any(step.status == "failed" for step in receipt.steps):
                raise ReceiptSchemaError("failed receipt lacks a failed step")
        case unreachable:
            assert_never(unreachable)


def validate_root_transition(current: ReceiptStatus, updated: ReceiptStatus) -> None:
    legal = {
        "planned": {"running", "blocked", "failed"},
        "running": {"running", "completed", "blocked", "failed"},
        "blocked": set(),
        "completed": set(),
        "failed": set(),
    }
    if updated not in legal[current]:
        raise ReceiptSchemaError("receipt root status transition is illegal")


def validate_step_transition(current: StepStatus, updated: StepStatus) -> None:
    legal = {
        "pending": {"pending", "verified", "blocked", "failed"},
        "intent": {"intent", "submitted", "verified", "blocked", "failed"},
        "submitted": {"submitted", "verified", "blocked", "failed"},
        "verified": {"verified"},
        "blocked": set(),
        "failed": set(),
    }
    if updated not in legal[current]:
        raise ReceiptSchemaError("receipt step status transition is illegal")


def validate_receipt_token(token: str) -> None:
    if (
        not token
        or len(token) > _MAX_TOKEN_LENGTH
        or any(character not in _TOKEN_CHARACTERS for character in token)
    ):
        raise ReceiptSchemaError("receipt cas_token is outside its canonical bound")


def _require_none(value: str | None, label: str) -> None:
    if value is not None:
        raise ReceiptSchemaError(f"{label} must be null")
