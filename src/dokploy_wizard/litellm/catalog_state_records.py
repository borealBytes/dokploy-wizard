from __future__ import annotations

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_state_types import (
    LkgRecord,
    StateObservation,
)
from dokploy_wizard.litellm.catalog_state_validation import (
    StateRecordError,
    boolean,
    digest,
    ids,
    integer,
    mapping,
    observation_status,
    optional_text,
    positive_integer,
    require_keys,
    sequence,
    source_name,
    text,
    timestamp,
    validate_source_provenance,
)
from dokploy_wizard.litellm.catalog_types import SourceProvenance


def parse_observation(value: JsonValue) -> StateObservation:
    data = mapping(value, "observation")
    require_keys(
        data,
        {
            "status", "observed_at", "complete", "source_ids", "source_ids_sha256",
            "source_ids_count", "accepted_ids", "accepted_ids_sha256",
            "accepted_ids_count", "decision_sha256", "provenance",
        },
        "observation",
    )
    source_ids = ids(data["source_ids"], "source_ids")
    accepted_ids = ids(data["accepted_ids"], "accepted_ids")
    if not set(accepted_ids).issubset(source_ids):
        raise StateRecordError("observation accepted ids are not source ids")
    if integer(data["source_ids_count"], "source_ids_count") != len(source_ids):
        raise StateRecordError("observation source id count is invalid")
    if integer(data["accepted_ids_count"], "accepted_ids_count") != len(accepted_ids):
        raise StateRecordError("observation accepted id count is invalid")
    if digest(data["source_ids_sha256"]) != sha256_bytes(canonical_json_bytes(list(source_ids))):
        raise StateRecordError("observation source id hash is invalid")
    if digest(data["accepted_ids_sha256"]) != sha256_bytes(
        canonical_json_bytes(list(accepted_ids))
    ):
        raise StateRecordError("observation accepted id hash is invalid")
    provenance = tuple(
        parse_provenance(item)
        for item in sequence(data["provenance"], "provenance")
    )
    return StateObservation(
        observation_status(text(data["status"], "status")),
        timestamp(data["observed_at"], "observed_at"),
        boolean(data["complete"], "complete"),
        source_ids,
        digest(data["source_ids_sha256"]),
        len(source_ids),
        accepted_ids,
        digest(data["accepted_ids_sha256"]),
        len(accepted_ids),
        digest(data["decision_sha256"]),
        provenance,
    )


def parse_provenance(value: JsonValue) -> SourceProvenance:
    data = mapping(value, "provenance")
    require_keys(
        data,
        {
            "source",
            "url",
            "content_type",
            "byte_count",
            "raw_sha256",
            "projected_sha256",
            "commit",
            "blob",
            "observed_at",
        },
        "provenance",
    )
    source = source_name(text(data["source"], "source"))
    commit = optional_text(data["commit"], "commit")
    blob = optional_text(data["blob"], "blob")
    validate_source_provenance(source, commit, blob)
    return SourceProvenance(
        source,
        text(data["url"], "url"),
        text(data["content_type"], "content_type"),
        integer(data["byte_count"], "byte_count"),
        digest(data["raw_sha256"]),
        digest(data["projected_sha256"]),
        commit,
        blob,
        timestamp(data["observed_at"], "observed_at"),
    )


def parse_lkg(value: JsonValue) -> LkgRecord:
    data = mapping(value, "lkg")
    require_keys(
        data,
        {
            "status",
            "generation",
            "accepted_at",
            "accepted_ids_sha256",
            "model_set_sha256",
            "decision_sha256",
            "artifact_path",
            "artifact_sha256",
        },
        "lkg",
    )
    status = text(data["status"], "lkg status")
    if status == "empty":
        result = LkgRecord.empty()
    elif status == "available":
        result = LkgRecord(
            "available",
            positive_integer(data["generation"], "generation"),
            timestamp(data["accepted_at"], "accepted_at"),
            digest(data["accepted_ids_sha256"]),
            digest(data["model_set_sha256"]),
            digest(data["decision_sha256"]),
            text(data["artifact_path"], "artifact_path"),
            digest(data["artifact_sha256"]),
        )
    else:
        raise StateRecordError("lkg status is invalid")
    return result
