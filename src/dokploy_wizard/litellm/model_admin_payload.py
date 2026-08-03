from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Final, Literal, assert_never
from uuid import UUID, uuid5

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_state_payload import timestamp_payload
from dokploy_wizard.litellm.catalog_types import ResolvedPrice, ResolvedPriceTier, ResolvedPricing
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminError,
    LiteLLMModelDeployment,
    LiteLLMOwnedModelInfo,
    LiteLLMPriceDimensions,
    LiteLLMPricing,
    LiteLLMPricingProvenance,
    LiteLLMRoutingParams,
    LiteLLMSourceProjection,
    ModelUuid,
    PricingZero,
)

PINNED_LITELLM_VERSION: Final = "v1.83.14-stable"
PINNED_LITELLM_COMMIT: Final = "3d2b8fed3281f60fcf6908c43df7823d6966897d"
PINNED_LITELLM_IMAGE: Final = (
    "ghcr.io/berriai/litellm@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
)
_MODEL_NAMESPACE: Final = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_SOURCE_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")
_OPENAI_API_BASE: Final = "https://opencode.ai/zen/go/v1"
_ANTHROPIC_API_BASE: Final = "https://opencode.ai/zen/go"
_UPSTREAM_API_KEY: Final = "os.environ/LITELLM_OPENCODE_GO_API_KEY"
_Dimension = Literal["input", "output", "cache_read", "cache_write"]


def build_owned_model_deployment(
    model: CatalogModel, *, bootstrap_static: bool
) -> LiteLLMModelDeployment:
    _validate_source_id(model.source_id)
    model_id = model_uuid_for_source(model.source_id)
    projection = model.projection
    source = LiteLLMSourceProjection(
        zen_url=projection.zen.url,
        zen_sha256=projection.zen.snapshot_sha256,
        zen_observed_at=_required_timestamp(projection.zen.observed_at),
        models_dev_url=projection.models_dev.url,
        models_dev_sha256=projection.models_dev.snapshot_sha256,
        models_dev_pricing_row_sha256=projection.models_dev.pricing_row_sha256,
        models_dev_provider_npm=projection.models_dev.provider_npm,
        models_dev_observed_at=_required_timestamp(projection.models_dev.observed_at),
        official_transport_url=projection.official_transport.url,
        official_transport_commit=projection.official_transport.commit,
        official_transport_blob_sha256=projection.official_transport.blob_sha256,
        official_transport_row_sha256=_required_row_sha256(
            projection.official_transport.row_sha256,
            "official transport",
        ),
        official_pricing_url=projection.official_pricing.url,
        official_pricing_commit=projection.official_pricing.commit,
        official_pricing_blob_sha256=projection.official_pricing.blob_sha256,
        official_pricing_row_sha256=_required_row_sha256(
            projection.official_pricing.row_sha256,
            "official pricing",
        ),
    )
    routing = LiteLLMRoutingParams(
        model=f"{model.transport}/{model.source_id}",
        api_base=_api_base(model.transport),
        api_key=_UPSTREAM_API_KEY,
    )
    provisional_info = LiteLLMOwnedModelInfo(
        model_id=model_id,
        source_id=model.source_id,
        transport=model.transport,
        source=source,
        pricing=_pricing(model.pricing, model.pricing_tiers_sha256, model.pricing_selection_sha256),
        merged_decision_sha256=model.merged_decision_sha256,
        max_input_tokens=model.limits.context,
        max_output_tokens=model.limits.output,
        bootstrap_static=bootstrap_static,
        spec_sha256="",
    )
    model_name = f"opencode-go/{model.source_id}"
    spec_payload: dict[str, JsonValue] = {
        "model_name": model_name,
        "litellm_params": routing.to_json(),
        "model_info": provisional_info.to_unhashed_json(),
    }
    model_info = replace(
        provisional_info,
        spec_sha256=sha256_bytes(canonical_json_bytes(spec_payload)),
    )
    return LiteLLMModelDeployment(
        model_name=model_name,
        litellm_params=routing,
        model_info=model_info,
    )


def _validate_source_id(source_id: str) -> None:
    is_canonical = (
        source_id.isascii()
        and source_id == source_id.lower()
        and _SOURCE_ID_PATTERN.fullmatch(source_id) is not None
    )
    if not is_canonical:
        raise LiteLLMModelAdminError(f"invalid canonical source id: {source_id!r}")


def model_uuid_for_source(source_id: str) -> ModelUuid:
    _validate_source_id(source_id)
    return ModelUuid(
        str(uuid5(_MODEL_NAMESPACE, f"dokploy-wizard/litellm/opencode-go/{source_id}"))
    )


def _required_timestamp(value: datetime) -> str:
    timestamp = timestamp_payload(value)
    if timestamp is None:
        raise LiteLLMModelAdminError("accepted source projection requires an observed timestamp")
    return timestamp


def _required_row_sha256(value: str | None, source_name: str) -> str:
    if value is None:
        raise LiteLLMModelAdminError(f"accepted model lacks {source_name} row provenance")
    return value


def _api_base(transport: Literal["openai", "anthropic"]) -> str:
    match transport:
        case "openai":
            return _OPENAI_API_BASE
        case "anthropic":
            return _ANTHROPIC_API_BASE
        case unreachable:
            assert_never(unreachable)


def _pricing(
    pricing: ResolvedPricing,
    tiers_sha256: str,
    selection_sha256: str,
) -> LiteLLMPricing:
    costs = LiteLLMPriceDimensions(
        input=pricing.scalar_input_per_token,
        output=pricing.scalar_output_per_token,
        cache_read=pricing.scalar_cache_read_per_token,
        cache_write=pricing.scalar_cache_write_per_token,
    )
    scalars = LiteLLMPriceDimensions(
        input=pricing.scalar_input_per_token * 1_000_000,
        output=pricing.scalar_output_per_token * 1_000_000,
        cache_read=pricing.scalar_cache_read_per_token * 1_000_000,
        cache_write=pricing.scalar_cache_write_per_token * 1_000_000,
    )
    return LiteLLMPricing(
        costs_per_token=costs,
        source_per_million=scalars,
        scalar_per_million=scalars,
        input_provenance=_pricing_provenance(pricing.tiers, "input", tiers_sha256),
        output_provenance=_pricing_provenance(pricing.tiers, "output", tiers_sha256),
        cache_read_provenance=_pricing_provenance(pricing.tiers, "cache_read", tiers_sha256),
        cache_write_provenance=_pricing_provenance(pricing.tiers, "cache_write", tiers_sha256),
        tiers_sha256=tiers_sha256,
        selection_sha256=selection_sha256,
    )


def _pricing_provenance(
    tiers: tuple[ResolvedPriceTier, ...], dimension: _Dimension, tiers_sha256: str
) -> LiteLLMPricingProvenance:
    selected = max((_price(tier, dimension) for tier in tiers), key=lambda price: price.value)
    _validate_provenance(selected, dimension)
    return LiteLLMPricingProvenance(
        selection=selected.provenance,
        selected_per_million=selected.value * 1_000_000,
        zero=_zero_kind(selected),
        tiers_sha256=tiers_sha256,
    )


def _price(tier: ResolvedPriceTier, dimension: _Dimension) -> ResolvedPrice:
    match dimension:
        case "input":
            return tier.input
        case "output":
            return tier.output
        case "cache_read":
            return tier.cache_read
        case "cache_write":
            return tier.cache_write
        case unreachable:
            assert_never(unreachable)


def _validate_provenance(price: ResolvedPrice, dimension: _Dimension) -> None:
    match dimension:
        case "input" | "output":
            if price.provenance == "input_fallback":
                raise LiteLLMModelAdminError(f"{dimension} pricing cannot use input fallback")
        case "cache_read" | "cache_write":
            return
        case unreachable:
            assert_never(unreachable)


def _zero_kind(price: ResolvedPrice) -> PricingZero:
    if price.value != 0:
        return "not_zero"
    match price.provenance:
        case "input_fallback":
            return "input_fallback_from_explicit_zero"
        case "max_both" | "official_only" | "models_dev_only":
            return "explicit_zero"
        case unreachable:
            assert_never(unreachable)
