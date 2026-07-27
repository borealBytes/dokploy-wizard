from __future__ import annotations

import json
import re
from datetime import datetime

from dokploy_wizard.litellm.catalog_clock import CatalogClock, read_catalog_clock
from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_models_dev import parse_model_fields
from dokploy_wizard.litellm.catalog_official import OfficialContractError, parse_official_document
from dokploy_wizard.litellm.catalog_types import (
    FetchDocument,
    ModelsDevModel,
    ModelsDevSnapshot,
    OfficialSnapshot,
    SourceContractError,
    SourceProvenance,
    ZenSnapshot,
)

_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def parse_zen(document: FetchDocument, clock: CatalogClock) -> ZenSnapshot:
    payload = _mapping(_parse_json(document.body), "Zen payload")
    if payload.get("object") != "list":
        raise SourceContractError("Zen object is invalid")
    records = _list(payload.get("data"), "Zen data")
    if not 1 <= len(records) <= 1000:
        raise SourceContractError("Zen record count is invalid")
    projected_records: dict[str, JsonValue] = {}
    for value in records:
        record = _mapping(value, "Zen record")
        source_id = _text(record.get("id"), "Zen id")
        created = record.get("created")
        valid = (
            _MODEL_ID.fullmatch(source_id) is not None
            and record.get("object") == "model"
            and isinstance(created, int)
            and not isinstance(created, bool)
            and record.get("owned_by") == "opencode"
        )
        if not valid:
            raise SourceContractError("Zen record is invalid")
        projected_records[source_id] = {
            "created": created,
            "id": source_id,
            "object": "model",
            "owned_by": "opencode",
        }
    if len(projected_records) != len(records):
        raise SourceContractError("Zen ids are not unique")
    ids = tuple(sorted(projected_records))
    projected: JsonValue = [projected_records[source_id] for source_id in ids]
    row_hashes = tuple(
        (source_id, sha256_bytes(canonical_json_bytes(projected_records[source_id])))
        for source_id in ids
    )
    return ZenSnapshot(
        ids,
        row_hashes,
        _provenance(document, projected, read_catalog_clock(clock)),
    )


def parse_models_dev(document: FetchDocument, clock: CatalogClock) -> ModelsDevSnapshot:
    payload = _mapping(_parse_json(document.body), "Models.dev payload")
    provider = _mapping(payload.get("opencode-go"), "Models.dev opencode-go provider")
    provider_id = _text(provider.get("id"), "Models.dev provider id")
    provider_npm = _text(provider.get("npm"), "Models.dev provider npm")
    if provider_id != "opencode-go":
        raise SourceContractError("Models.dev provider id is invalid")
    records = _mapping(provider.get("models"), "Models.dev models")
    if len(records) > 5000:
        raise SourceContractError("Models.dev model count is invalid")
    models: list[ModelsDevModel] = []
    projected_models: dict[str, JsonValue] = {}
    for source_id, value in sorted(records.items()):
        record = _mapping(value, "Models.dev model")
        if _text(record.get("id"), "Models.dev model id") != source_id:
            raise SourceContractError("Models.dev model key/id mismatch")
        fields = parse_model_fields(record)
        projected_model: JsonValue = {
            "cost": fields.pricing_payload,
            "id": source_id,
            "limit": fields.limits_payload,
            "modalities": fields.modalities_payload,
            "name": fields.name,
        }
        models.append(
            ModelsDevModel(
                source_id,
                fields.name,
                fields.pricing,
                fields.pricing_valid,
                fields.limits,
                fields.limits_valid,
                fields.modalities,
                sha256_bytes(canonical_json_bytes(projected_model)),
                sha256_bytes(canonical_json_bytes(fields.pricing_payload)),
            )
        )
        projected_models[source_id] = projected_model
    projected: JsonValue = {
        "id": provider_id,
        "models": projected_models,
        "npm": provider_npm,
    }
    return ModelsDevSnapshot(
        provider_id,
        provider_npm,
        tuple(models),
        _provenance(document, projected, read_catalog_clock(clock)),
    )


def parse_official(document: FetchDocument, clock: CatalogClock) -> OfficialSnapshot:
    try:
        return parse_official_document(document, read_catalog_clock(clock))
    except OfficialContractError as error:
        raise SourceContractError(error.reason) from error


def _parse_json(body: bytes) -> JsonValue:
    def reject_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise SourceContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise SourceContractError(f"non-finite JSON number: {value}")

    try:
        decoded: JsonValue = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceContractError("source JSON is invalid") from error
    return decoded


def _mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise SourceContractError(f"{label} must be an object")
    return value


def _list(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise SourceContractError(f"{label} must be a list")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceContractError(f"{label} must be text")
    return value.strip()


def _provenance(
    document: FetchDocument,
    projected: JsonValue,
    observed_at: datetime,
) -> SourceProvenance:
    return SourceProvenance(
        document.spec.name,
        document.spec.url,
        document.content_type,
        len(document.body),
        sha256_bytes(document.body),
        sha256_bytes(canonical_json_bytes(projected)),
        document.spec.commit,
        document.spec.blob,
        observed_at,
    )
