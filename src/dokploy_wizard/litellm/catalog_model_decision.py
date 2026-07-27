from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel, CatalogQuarantine
from dokploy_wizard.litellm.catalog_projection import ModelDecisionProjection
from dokploy_wizard.litellm.catalog_state_payload import timestamp_payload
from dokploy_wizard.litellm.catalog_types import (
    Modalities,
    ModelLimits,
    ResolvedPricing,
    TransportName,
)


@dataclass(frozen=True, slots=True)
class CatalogModelDraft:
    source_id: str
    name: str
    transport: TransportName
    endpoint: str
    package: str
    pricing: ResolvedPricing
    limits: ModelLimits
    modalities: Modalities
    projection: ModelDecisionProjection


def finalize_catalog_model(draft: CatalogModelDraft) -> CatalogModel:
    pricing_tiers_sha256 = sha256_bytes(
        canonical_json_bytes(_pricing_tiers_payload(draft.pricing))
    )
    pricing_selection_sha256 = sha256_bytes(
        canonical_json_bytes(_pricing_payload(draft.pricing))
    )
    decision_payload: JsonValue = {
        "endpoint": draft.endpoint,
        "limits": _limits_payload(draft.limits),
        "modalities": _modalities_payload(draft.modalities),
        "name": draft.name,
        "package": draft.package,
        "pricing": _pricing_payload(draft.pricing),
        "projection": _decision_projection_payload(draft.projection),
        "source_id": draft.source_id,
        "transport": draft.transport,
    }
    return CatalogModel(
        draft.source_id,
        draft.name,
        draft.transport,
        draft.endpoint,
        draft.package,
        draft.pricing,
        draft.limits,
        draft.modalities,
        pricing_tiers_sha256,
        pricing_selection_sha256,
        draft.projection,
        sha256_bytes(canonical_json_bytes(decision_payload)),
    )


def catalog_model_payload(model: CatalogModel) -> JsonValue:
    return {
        "endpoint": model.endpoint,
        "limits": _limits_payload(model.limits),
        "merged_decision_sha256": model.merged_decision_sha256,
        "modalities": _modalities_payload(model.modalities),
        "name": model.name,
        "package": model.package,
        "pricing": _pricing_payload(model.pricing),
        "pricing_selection_sha256": model.pricing_selection_sha256,
        "pricing_tiers_sha256": model.pricing_tiers_sha256,
        "projection": _projection_payload(model.projection),
        "source_id": model.source_id,
        "transport": model.transport,
    }


def quarantine_payload(record: CatalogQuarantine) -> JsonValue:
    return {
        "preserved_visible_row_sha256": record.preserved_visible_row_sha256,
        "reason": record.reason,
        "source_id": record.source_id,
        "source_sha256": record.source_sha256,
    }


def _pricing_payload(pricing: ResolvedPricing) -> JsonValue:
    return {
        "scalar_cache_read_per_token": pricing.scalar_cache_read_per_token,
        "scalar_cache_write_per_token": pricing.scalar_cache_write_per_token,
        "scalar_input_per_token": pricing.scalar_input_per_token,
        "scalar_output_per_token": pricing.scalar_output_per_token,
        "tiers": [
            {
                "cache_read": [tier.cache_read.value, tier.cache_read.provenance],
                "cache_write": [tier.cache_write.value, tier.cache_write.provenance],
                "input": [tier.input.value, tier.input.provenance],
                "name": tier.name,
                "output": [tier.output.value, tier.output.provenance],
            }
            for tier in pricing.tiers
        ],
    }


def _pricing_tiers_payload(pricing: ResolvedPricing) -> JsonValue:
    return [
        {
            "cache_read": tier.cache_read.value,
            "cache_write": tier.cache_write.value,
            "input": tier.input.value,
            "name": tier.name,
            "output": tier.output.value,
        }
        for tier in pricing.tiers
    ]


def _projection_payload(value: ModelDecisionProjection) -> JsonValue:
    payload = _decision_projection_payload(value)
    assert isinstance(payload, dict)
    models_dev = payload["models_dev"]
    zen = payload["zen"]
    assert isinstance(models_dev, dict)
    assert isinstance(zen, dict)
    models_dev["observed_at"] = timestamp_payload(value.models_dev.observed_at)
    zen["observed_at"] = timestamp_payload(value.zen.observed_at)
    return payload


def _decision_projection_payload(value: ModelDecisionProjection) -> JsonValue:
    return {
        "models_dev": {
            "pricing_row_sha256": value.models_dev.pricing_row_sha256,
            "provider_npm": value.models_dev.provider_npm,
            "row_sha256": value.models_dev.row_sha256,
            "snapshot_sha256": value.models_dev.snapshot_sha256,
            "url": value.models_dev.url,
        },
        "official_pricing": {
            "blob_sha256": value.official_pricing.blob_sha256,
            "commit": value.official_pricing.commit,
            "row_sha256": value.official_pricing.row_sha256,
            "snapshot_sha256": value.official_pricing.snapshot_sha256,
            "url": value.official_pricing.url,
        },
        "official_transport": {
            "blob_sha256": value.official_transport.blob_sha256,
            "commit": value.official_transport.commit,
            "row_sha256": value.official_transport.row_sha256,
            "snapshot_sha256": value.official_transport.snapshot_sha256,
            "url": value.official_transport.url,
        },
        "zen": {
            "row_sha256": value.zen.row_sha256,
            "snapshot_sha256": value.zen.snapshot_sha256,
            "url": value.zen.url,
        },
    }


def _limits_payload(value: ModelLimits) -> JsonValue:
    return {"context": value.context, "output": value.output}


def _modalities_payload(value: Modalities) -> JsonValue:
    return {"input": list(value.input), "output": list(value.output)}
