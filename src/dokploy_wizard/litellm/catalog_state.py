from __future__ import annotations

import json

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_state_invariants import validate_catalog_state
from dokploy_wizard.litellm.catalog_state_lifecycle_records import (
    parse_anomaly,
    parse_missing,
    parse_quarantine,
    parse_result,
)
from dokploy_wizard.litellm.catalog_state_payload import catalog_state_payload
from dokploy_wizard.litellm.catalog_state_records import (
    parse_lkg,
    parse_observation,
)
from dokploy_wizard.litellm.catalog_state_types import (
    CatalogState,
    CatalogStateTransition,
    LkgRecord,
)
from dokploy_wizard.litellm.catalog_state_validation import (
    StateRecordError,
    integer,
    mapping,
    optional_digest,
    optional_timestamp,
    require_keys,
    runtime_state,
    sequence,
    text,
)
from dokploy_wizard.litellm.catalog_types import ObservationStatus

_STATE_KEYS = {
    "schema_version", "sync_contract_version", "catalog_id", "state",
    "last_attempt_at", "last_success_at", "last_input_sha256", "last_output_sha256",
    "durable_write_count", "observation", "lkg", "anomaly", "missing", "quarantine",
    "last_result",
}


class CatalogStateError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


def empty_catalog_state(catalog_id: str) -> CatalogState:
    if not catalog_id:
        raise CatalogStateError("catalog id must not be empty")
    return CatalogState(
        1,
        2,
        catalog_id,
        "enabled",
        None,
        None,
        None,
        None,
        0,
        None,
        LkgRecord.empty(),
        AnomalyRecord.empty(),
        (),
        (),
        None,
    )


def state_bytes(state: CatalogState) -> bytes:
    return canonical_json_bytes(catalog_state_payload(state))


def parse_catalog_state(raw: bytes) -> CatalogState:
    try:
        decoded: JsonValue = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        data = mapping(decoded, "state")
        require_keys(data, _STATE_KEYS, "state")
        if data["schema_version"] != 1 or data["sync_contract_version"] != 2:
            raise CatalogStateError("state version is unsupported")
        parsed_runtime_state = runtime_state(text(data["state"], "state"))
        observation = (
            None
            if data["observation"] is None
            else parse_observation(data["observation"])
        )
        if observation is not None and tuple(
            item.source for item in observation.provenance
        ) != ("zen", "models_dev", "official"):
            raise CatalogStateError("observation provenance is incomplete")
        last_result = None if data["last_result"] is None else parse_result(data["last_result"])
        result = CatalogState(
            1,
            2,
            text(data["catalog_id"], "catalog_id"),
            parsed_runtime_state,
            optional_timestamp(data["last_attempt_at"], "last_attempt_at"),
            optional_timestamp(data["last_success_at"], "last_success_at"),
            optional_digest(data["last_input_sha256"]),
            optional_digest(data["last_output_sha256"]),
            integer(data["durable_write_count"], "durable_write_count"),
            observation,
            parse_lkg(data["lkg"]),
            parse_anomaly(data["anomaly"]),
            tuple(parse_missing(item) for item in sequence(data["missing"], "missing")),
            tuple(parse_quarantine(item) for item in sequence(data["quarantine"], "quarantine")),
            last_result,
        )
        validate_catalog_state(result)
    except (UnicodeDecodeError, json.JSONDecodeError, StateRecordError) as error:
        raise CatalogStateError(str(error)) from error
    if state_bytes(result) != raw:
        raise CatalogStateError("state bytes are not canonical")
    return result


def rejected_transition(
    previous: CatalogState,
    status: ObservationStatus,
) -> CatalogStateTransition:
    if status not in {"rejected_invalid", "rejected_clock_regression", "quarantined_anomalous"}:
        raise CatalogStateError("rejected transition status is invalid")
    return CatalogStateTransition(previous, status, False, 0)


def _unique_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogStateError(f"duplicate state key: {key}")
        result[key] = value
    return result
