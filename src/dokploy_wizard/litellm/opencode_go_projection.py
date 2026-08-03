from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.model_admin_payload import model_uuid_for_source
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    ModelUuid,
)

OWNED_MODEL_INFO_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "blocked",
    "mode",
    "managed_by",
    "managed_catalog",
    "source_id",
    "transport",
    "zen_url",
    "zen_sha256",
    "zen_observed_at",
    "models_dev_url",
    "models_dev_sha256",
    "models_dev_pricing_row_sha256",
    "models_dev_provider_npm",
    "models_dev_observed_at",
    "official_transport_url",
    "official_transport_commit",
    "official_transport_blob_sha256",
    "official_transport_row_sha256",
    "official_pricing_url",
    "official_pricing_commit",
    "official_pricing_blob_sha256",
    "official_pricing_row_sha256",
    "merged_decision_sha256",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "max_input_tokens",
    "max_output_tokens",
    "dokploy_source_input_per_million",
    "dokploy_source_output_per_million",
    "dokploy_source_cache_read_per_million",
    "dokploy_source_cache_write_per_million",
    "dokploy_scalar_input_per_million",
    "dokploy_scalar_output_per_million",
    "dokploy_scalar_cache_read_per_million",
    "dokploy_scalar_cache_write_per_million",
    "dokploy_pricing_tiers_sha256",
    "dokploy_pricing_selection_sha256",
    "dokploy_pricing_input_provenance",
    "dokploy_pricing_output_provenance",
    "dokploy_pricing_cache_read_provenance",
    "dokploy_pricing_cache_write_provenance",
    "dokploy_spec_sha256",
    "bootstrap_static",
)


@dataclass(frozen=True, slots=True)
class OpenCodeGoOwnedProjection:
    model_name: str
    model_id: ModelUuid
    fingerprint: str


def deployment_projection(deployment: LiteLLMModelDeployment) -> OpenCodeGoOwnedProjection:
    model_info = deployment.model_info.to_json()
    _require_exact_info_keys(model_info)
    return OpenCodeGoOwnedProjection(
        model_name=deployment.model_name,
        model_id=deployment.model_id,
        fingerprint=_fingerprint(
            {
                "model_name": deployment.model_name,
                "litellm_params": deployment.litellm_params.to_json(),
                "model_info": {key: model_info[key] for key in OWNED_MODEL_INFO_FIELDS},
            }
        ),
    )


def record_projection(
    record: LiteLLMModelRecord, deployment: LiteLLMModelDeployment
) -> OpenCodeGoOwnedProjection:
    _require_exact_info_keys(record.model_info)
    if record.model_name != deployment.model_name or record.model_id != deployment.model_id:
        raise LiteLLMModelAdminConflict("owned record identity does not match desired deployment")
    if record.model_info["id"] != record.model_id:
        raise LiteLLMModelAdminConflict("owned record model_info id does not match row id")
    api_key = (
        deployment.litellm_params.api_key
        if record.litellm_params.api_key is None
        else record.litellm_params.api_key
    )
    return OpenCodeGoOwnedProjection(
        model_name=record.model_name,
        model_id=record.model_id,
        fingerprint=_fingerprint(
            {
                "model_name": record.model_name,
                "litellm_params": {
                    "model": record.litellm_params.model,
                    "api_base": record.litellm_params.api_base,
                    "api_key": api_key,
                },
                "model_info": {
                    key: record.model_info[key] for key in OWNED_MODEL_INFO_FIELDS
                },
            }
        ),
    )


def owner_source_id(record: LiteLLMModelRecord) -> str | None:
    source_id = record.model_info.get("source_id")
    if not isinstance(source_id, str):
        return None
    try:
        expected_id = model_uuid_for_source(source_id)
    except LiteLLMModelAdminError:
        return None
    if (
        record.model_name != f"opencode-go/{source_id}"
        or record.model_id != expected_id
        or record.model_info.get("id") != expected_id
        or record.model_info.get("managed_by") != "dokploy-wizard"
        or record.model_info.get("managed_catalog") != "opencode-go"
        or record.model_info.get("mode") != "chat"
        or record.model_info.get("blocked") is not False
    ):
        return None
    return source_id


def inventory_fingerprint(records: tuple[LiteLLMModelRecord, ...]) -> str:
    rows: list[JsonValue] = []
    for record in sorted(
        (item for item in records if item.model_name.startswith("opencode-go/")),
        key=lambda item: (item.model_name, item.model_id),
    ):
        _require_exact_info_keys(record.model_info)
        rows.append(
            {
                "model_name": record.model_name,
                "litellm_params": {
                    "model": record.litellm_params.model,
                    "api_base": record.litellm_params.api_base,
                    "api_key": record.litellm_params.api_key,
                },
                "model_info": {
                    key: record.model_info[key] for key in OWNED_MODEL_INFO_FIELDS
                },
            }
        )
    return sha256_bytes(canonical_json_bytes(rows))


def _require_exact_info_keys(model_info: dict[str, JsonValue]) -> None:
    missing = tuple(key for key in OWNED_MODEL_INFO_FIELDS if key not in model_info)
    if missing:
        raise LiteLLMModelAdminConflict(f"owned model_info is missing fields: {missing}")


def _fingerprint(payload: JsonValue) -> str:
    return sha256_bytes(canonical_json_bytes(payload))
