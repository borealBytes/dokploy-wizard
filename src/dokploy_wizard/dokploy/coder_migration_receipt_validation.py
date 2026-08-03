from __future__ import annotations

import json

from dokploy_wizard.dokploy.coder_migration_receipt_schema import parse_receipt_value
from dokploy_wizard.dokploy.coder_migration_receipt_semantics import validate_receipt_semantics
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    ReceiptSchemaError,
    receipt_bytes,
)
from dokploy_wizard.dokploy.coder_migration_types import JsonValue


def parse_receipt(payload: bytes) -> MigrationReceipt:
    try:
        value: JsonValue = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptSchemaError("receipt is not JSON") from error
    receipt = parse_receipt_value(value)
    validate_receipt_semantics(receipt)
    if receipt_bytes(receipt) != payload:
        raise ReceiptSchemaError("receipt is not canonical JSON")
    return receipt
