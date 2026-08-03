from __future__ import annotations

import json

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.opencode_go_cutover_receipt import (
    canonical_receipt_bytes,
    parse_receipt,
    receipt_to_dict,
)
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverImage,
    CutoverReceipt,
    CutoverRollback,
    CutoverVerification,
)


def test_receipt_facade_preserves_canonical_byte_roundtrip() -> None:
    # Given
    image = CutoverImage(None, None, None)
    receipt = CutoverReceipt(
        operation_id="operation-1",
        owner_id="owner-1",
        catalog_id="catalog-1",
        status="intent",
        compose_id="compose-1",
        pre_image=image,
        transitional_image=image,
        dynamic_image=image,
        rows=(),
        verification=CutoverVerification(False, False, False, None),
        rollback=CutoverRollback("not_needed", None, None, None),
        updated_at="2026-07-31T00:00:00Z",
    )
    expected: dict[str, JsonValue] = {
        "schema_version": 1,
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "catalog_id": "catalog-1",
        "status": "intent",
        "compose_id": "compose-1",
        "pre_image": {
            "compose_sha256": None,
            "config_sha256": None,
            "model_set_sha256": None,
        },
        "transitional_image": {
            "compose_sha256": None,
            "config_sha256": None,
            "model_set_sha256": None,
        },
        "dynamic_image": {
            "compose_sha256": None,
            "config_sha256": None,
            "model_set_sha256": None,
        },
        "rows": [],
        "verification": {
            "transitional_verified": False,
            "visibility_verified": False,
            "dynamic_verified": False,
            "aliases_sha256": None,
        },
        "rollback": {
            "status": "not_needed",
            "pre_fingerprint": None,
            "post_fingerprint": None,
            "current_fingerprint": None,
        },
        "updated_at": "2026-07-31T00:00:00Z",
    }

    # When
    payload = canonical_receipt_bytes(receipt)
    parsed = parse_receipt(payload)

    # Then
    assert receipt_to_dict(receipt) == expected
    assert payload == (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert parsed == receipt
