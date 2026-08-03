"""Receipt-row construction and fingerprint helpers for OpenCode Go cutovers."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import assert_never

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
)
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverReceipt,
    CutoverRow,
    OpenCodeGoCutoverBlockedError,
)
from dokploy_wizard.litellm.opencode_go_plan import (
    CreateIntent,
    DeleteIntent,
    NoopIntent,
    OpenCodeGoReconciliationInput,
    OpenCodeGoReconciliationIntent,
    UpdateIntent,
)
from dokploy_wizard.litellm.opencode_go_projection import (
    inventory_fingerprint,
    record_projection,
)


def rows_for_intents(
    intents: tuple[OpenCodeGoReconciliationIntent, ...],
) -> tuple[CutoverRow, ...]:
    rows = (_row_from_intent(intent) for intent in intents)
    return tuple(sorted(rows, key=lambda row: row.model_name))


def aliases_for(reconciliation_input: OpenCodeGoReconciliationInput) -> tuple[str, ...]:
    return tuple(f"opencode-go/{model.source_id}" for model in reconciliation_input.models)


def rows_fingerprint(rows: tuple[CutoverRow, ...]) -> str:
    payload = "\n".join(f"{row.model_id}:{row.current_fingerprint}" for row in rows)
    return sha256(payload.encode()).hexdigest()


def intent_model_id(intent: OpenCodeGoReconciliationIntent) -> str:
    match intent:
        case (
            CreateIntent(deployment=deployment)
            | UpdateIntent(deployment=deployment)
            | NoopIntent(deployment=deployment)
        ):
            return str(deployment.model_id)
        case DeleteIntent(previous=previous):
            return str(previous.model_id)
        case unreachable:
            assert_never(unreachable)


def intent_model_name(intent: OpenCodeGoReconciliationIntent) -> str:
    match intent:
        case (
            CreateIntent(deployment=deployment)
            | UpdateIntent(deployment=deployment)
            | NoopIntent(deployment=deployment)
        ):
            return deployment.model_name
        case DeleteIntent(previous=previous):
            return previous.model_name
        case unreachable:
            assert_never(unreachable)


def row_for(receipt: CutoverReceipt, model_id: str) -> CutoverRow:
    for row in receipt.rows:
        if row.model_id == model_id:
            return row
    raise OpenCodeGoCutoverBlockedError("cutover receipt row is missing")


def replace_row(receipt: CutoverReceipt, row: CutoverRow, updated_at: str) -> CutoverReceipt:
    rows = tuple(item if item.model_id != row.model_id else row for item in receipt.rows)
    return replace(receipt, rows=rows, updated_at=updated_at)


def request_sha256(deployment: LiteLLMModelDeployment) -> str:
    return sha256_bytes(canonical_json_bytes(deployment.to_api_payload()))


def response_sha256(record: LiteLLMModelRecord) -> str:
    payload: dict[str, JsonValue] = {
        "model_id": record.model_id,
        "model_name": record.model_name,
        "litellm_params": {
            "model": record.litellm_params.model,
            "api_base": record.litellm_params.api_base,
            "api_key": record.litellm_params.api_key,
        },
        "model_info": record.model_info,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def inventory_row_fingerprint(record: LiteLLMModelRecord) -> str:
    return inventory_fingerprint((record,))


def _row_from_intent(intent: OpenCodeGoReconciliationIntent) -> CutoverRow:
    match intent:
        case CreateIntent(deployment=deployment, expected_fingerprint=expected):
            return CutoverRow(
                str(deployment.model_id),
                deployment.model_name,
                "create",
                "intent",
                None,
                expected,
                None,
                request_sha256(deployment),
                None,
                deployment.model_info.bootstrap_static,
                None,
            )
        case UpdateIntent(
            deployment=deployment,
            previous=previous,
            expected_fingerprint=expected,
        ):
            previous_fingerprint = record_projection(previous, deployment).fingerprint
            return CutoverRow(
                str(deployment.model_id),
                deployment.model_name,
                "update",
                "intent",
                previous_fingerprint,
                expected,
                previous_fingerprint,
                request_sha256(deployment),
                None,
                deployment.model_info.bootstrap_static,
                None,
            )
        case NoopIntent(
            deployment=deployment,
            previous=previous,
            expected_fingerprint=expected,
        ):
            previous_fingerprint = record_projection(previous, deployment).fingerprint
            return CutoverRow(
                str(deployment.model_id),
                deployment.model_name,
                "noop",
                "intent",
                previous_fingerprint,
                expected,
                previous_fingerprint,
                None,
                None,
                deployment.model_info.bootstrap_static,
                None,
            )
        case DeleteIntent(previous=previous):
            previous_fingerprint = inventory_row_fingerprint(previous)
            return CutoverRow(
                str(previous.model_id),
                previous.model_name,
                "delete",
                "intent",
                previous_fingerprint,
                None,
                previous_fingerprint,
                sha256(str(previous.model_id).encode()).hexdigest(),
                None,
                bool(previous.model_info.get("bootstrap_static")),
                None,
            )
        case unreachable:
            assert_never(unreachable)
