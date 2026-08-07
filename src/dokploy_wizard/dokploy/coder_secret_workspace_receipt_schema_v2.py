from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import replace
from typing import Final

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_types import (
    MAX_CREATE_ATTEMPTS,
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
)

_KEYS: Final = frozenset(
    {
        "create_attempt_limit",
        "predecessor",
        "protocol_fingerprint",
        "protocol_revision",
        "schema_version",
        "successor",
    }
)
_PREDECESSOR_KEYS: Final = frozenset(
    {"protocol_revision", "receipt_base64", "receipt_sha256"}
)
CORRECTED_CREATE_PROTOCOL_FINGERPRINT: Final = hashlib.sha256(
    b"coder-create-v2:no-wait:parameter-defaults"
).hexdigest()


def parse_v2_receipt(mapping: dict[str, JsonValue]) -> WorkspaceVerificationReceipt:
    from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import (
        parse_receipt_bytes,
    )

    if (
        frozenset(mapping) != _KEYS
        or mapping.get("schema_version") != 2
        or mapping.get("protocol_revision") != "corrected-create-v2"
        or mapping.get("protocol_fingerprint") != CORRECTED_CREATE_PROTOCOL_FINGERPRINT
        or mapping.get("create_attempt_limit") != MAX_CREATE_ATTEMPTS
    ):
        raise _invalid()
    predecessor = mapping.get("predecessor")
    successor = mapping.get("successor")
    if not isinstance(predecessor, dict) or not isinstance(successor, dict):
        raise _invalid()
    if (
        frozenset(predecessor) != _PREDECESSOR_KEYS
        or predecessor.get("protocol_revision") != "legacy-create-v1"
    ):
        raise _invalid()
    encoded = predecessor.get("receipt_base64")
    digest = predecessor.get("receipt_sha256")
    if not isinstance(encoded, str) or not isinstance(digest, str):
        raise _invalid()
    try:
        parent_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _invalid() from error
    if hashlib.sha256(parent_bytes).hexdigest() != digest:
        raise _invalid()
    parent = parse_receipt_bytes(parent_bytes)
    if not _is_exhausted_legacy(parent):
        raise _invalid()
    successor_bytes = json.dumps(successor, sort_keys=True, separators=(",", ":")).encode()
    parsed = parse_receipt_bytes(successor_bytes)
    if not _same_intent(parent, parsed):
        raise _invalid()
    return replace(parsed, protocol_revision=2, predecessor_receipt_bytes=parent_bytes)


def v2_receipt_bytes(receipt: WorkspaceVerificationReceipt) -> bytes:
    from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import receipt_bytes

    parent_bytes = receipt.predecessor_receipt_bytes
    if parent_bytes is None:
        raise _invalid()
    parent = parse_v2_parent(parent_bytes)
    successor = replace(
        receipt,
        protocol_revision=1,
        predecessor_receipt_bytes=None,
    )
    if not _same_intent(parent, successor):
        raise _invalid()
    successor_payload: JsonValue = json.loads(receipt_bytes(successor))
    return json.dumps(
        {
            "create_attempt_limit": MAX_CREATE_ATTEMPTS,
            "predecessor": {
                "protocol_revision": "legacy-create-v1",
                "receipt_base64": base64.b64encode(parent_bytes).decode(),
                "receipt_sha256": hashlib.sha256(parent_bytes).hexdigest(),
            },
            "protocol_revision": "corrected-create-v2",
            "protocol_fingerprint": CORRECTED_CREATE_PROTOCOL_FINGERPRINT,
            "schema_version": 2,
            "successor": successor_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def parse_v2_parent(payload: bytes) -> WorkspaceVerificationReceipt:
    from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import (
        parse_receipt_bytes,
    )

    parent = parse_receipt_bytes(payload)
    if not _is_exhausted_legacy(parent):
        raise _invalid()
    return parent


def _is_exhausted_legacy(receipt: WorkspaceVerificationReceipt) -> bool:
    return (
        receipt.protocol_revision == 1
        and receipt.predecessor_receipt_bytes is None
        and receipt.phase is WorkspaceVerificationPhase.FAILED
        and receipt.failure_reason == "create_retry_exhausted"
        and receipt.create_attempts == MAX_CREATE_ATTEMPTS
        and receipt.workspace_id is None
        and receipt.workspace_owner_id is None
        and receipt.workspace_owner_name is None
        and receipt.observed_value_sha256 is None
    )


def _same_intent(
    parent: WorkspaceVerificationReceipt, successor: WorkspaceVerificationReceipt
) -> bool:
    return (
        parent.owner_id == successor.owner_id
        and parent.workspace_name == successor.workspace_name
        and parent.template_id == successor.template_id
        and parent.template_name == successor.template_name
        and parent.env_name == successor.env_name
        and parent.expected_value_sha256 == successor.expected_value_sha256
    )


def _invalid() -> CoderSecretClientError:
    return CoderSecretClientError(
        "Coder workspace verification receipt is invalid",
        kind="client_workspace_receipt_invalid",
    )
