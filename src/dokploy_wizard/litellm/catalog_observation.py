from __future__ import annotations

from typing import Literal, TypeAlias, assert_never

from dokploy_wizard.litellm.catalog_clock import CatalogClock, read_catalog_clock
from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_model_decision import (
    CatalogModelDraft,
    catalog_model_payload,
    finalize_catalog_model,
    quarantine_payload,
)
from dokploy_wizard.litellm.catalog_observation_types import (
    CatalogModel,
    CatalogObservation,
    CatalogQuarantine,
)
from dokploy_wizard.litellm.catalog_observation_types import CatalogSources as CatalogSources
from dokploy_wizard.litellm.catalog_official import (
    official_pricing_for,
    official_pricing_rows_for,
)
from dokploy_wizard.litellm.catalog_pricing import PricingContractError, merge_pricing
from dokploy_wizard.litellm.catalog_projection import (
    ModelDecisionProjection,
    ModelsDevModelProjection,
    OfficialModelProjection,
    ZenModelProjection,
)
from dokploy_wizard.litellm.catalog_quarantine import QuarantineCandidate, quarantine_model
from dokploy_wizard.litellm.catalog_types import (
    ModelsDevModel,
    ObservationStatus,
    OfficialEndpoint,
    OfficialPriceRow,
    QuarantineReason,
    TransportName,
)

_OPENAI_ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
_ANTHROPIC_ENDPOINT = "https://opencode.ai/zen/go/v1/messages"
OpenAITransportKey: TypeAlias = tuple[
    Literal["https://opencode.ai/zen/go/v1/chat/completions"],
    Literal["@ai-sdk/openai-compatible"],
]
AnthropicTransportKey: TypeAlias = tuple[
    Literal["https://opencode.ai/zen/go/v1/messages"],
    Literal["@ai-sdk/anthropic"],
]
KnownTransportKey: TypeAlias = OpenAITransportKey | AnthropicTransportKey


def build_observation(
    sources: CatalogSources,
    clock: CatalogClock,
    previous_visible: tuple[CatalogModel, ...] = (),
) -> CatalogObservation:
    observed_at = read_catalog_clock(clock)
    zen = sources.zen
    models_dev = sources.models_dev
    official = sources.official
    endpoints = {row.source_id: row for row in official.endpoints}
    previous = {row.source_id: row for row in previous_visible}
    accepted: list[CatalogModel] = []
    preserved: list[CatalogModel] = []
    quarantined: list[CatalogQuarantine] = []
    for source_id in zen.ids:
        endpoint = endpoints.get(source_id)
        model = models_dev.model_for(source_id)
        reason = _quarantine_reason(endpoint, model)
        if reason is not None:
            preserved_model, quarantine = quarantine_model(
                QuarantineCandidate(
                    source_id,
                    reason,
                    endpoint,
                    model,
                    previous.get(source_id),
                ),
                sources,
            )
            if preserved_model is not None:
                preserved.append(preserved_model)
            quarantined.append(quarantine)
            continue
        if endpoint is None or model is None or model.limits is None or model.pricing is None:
            raise AssertionError("validated catalog inputs are absent")
        official_pricing = official_pricing_for(official, endpoint.model_name)
        official_pricing_rows = official_pricing_rows_for(official, endpoint.model_name)
        try:
            pricing = merge_pricing(official_pricing, model.pricing)
        except PricingContractError:
            preserved_model, quarantine = quarantine_model(
                QuarantineCandidate(
                    source_id,
                    "invalid_pricing",
                    endpoint,
                    model,
                    previous.get(source_id),
                ),
                sources,
            )
            if preserved_model is not None:
                preserved.append(preserved_model)
            quarantined.append(quarantine)
            continue
        zen_row_sha256 = zen.row_sha256_for(source_id)
        if (
            zen_row_sha256 is None
            or official.provenance.commit is None
            or official.provenance.blob is None
        ):
            raise AssertionError("validated source provenance is absent")
        official_pricing_sha256 = _official_pricing_sha256(official_pricing_rows)
        projection = ModelDecisionProjection(
            ZenModelProjection(
                zen.provenance.url,
                zen.provenance.projected_sha256,
                zen_row_sha256,
                zen.provenance.observed_at,
            ),
            ModelsDevModelProjection(
                models_dev.provenance.url,
                models_dev.provenance.projected_sha256,
                model.projected_row_sha256,
                model.pricing_row_sha256,
                models_dev.provider_npm,
                models_dev.provenance.observed_at,
            ),
            OfficialModelProjection(
                official.provenance.url,
                official.provenance.commit,
                official.provenance.blob,
                official.provenance.projected_sha256,
                endpoint.row_sha256,
            ),
            OfficialModelProjection(
                official.provenance.url,
                official.provenance.commit,
                official.provenance.blob,
                official.provenance.projected_sha256,
                official_pricing_sha256,
            ),
        )
        accepted.append(
            finalize_catalog_model(
                CatalogModelDraft(
                source_id=source_id,
                name=model.name,
                transport=_transport(endpoint),
                endpoint=endpoint.endpoint,
                package=endpoint.package,
                pricing=pricing,
                limits=model.limits,
                modalities=model.modalities,
                    projection=projection,
                )
            )
        )
    accepted.sort(key=lambda item: item.source_id)
    preserved.sort(key=lambda item: item.source_id)
    quarantined.sort(key=lambda item: item.source_id)
    accepted_ids = tuple(item.source_id for item in accepted)
    if not accepted_ids:
        raise PricingContractError("catalog has no valid intersection")
    status: ObservationStatus = "accepted_with_quarantine" if quarantined else "accepted"
    decision_payload: JsonValue = {
        "accepted": [catalog_model_payload(item) for item in accepted],
        "official_projected_sha256": official.provenance.projected_sha256,
        "models_dev_projected_sha256": models_dev.provenance.projected_sha256,
        "preserved": [catalog_model_payload(item) for item in preserved],
        "quarantine": [quarantine_payload(item) for item in quarantined],
        "zen_projected_sha256": zen.provenance.projected_sha256,
    }
    return CatalogObservation(
        status=status,
        observed_at=observed_at,
        complete=True,
        source_ids=zen.ids,
        accepted_ids=accepted_ids,
        accepted_models=tuple(accepted),
        preserved_models=tuple(preserved),
        quarantine=tuple(quarantined),
        source_ids_sha256=sha256_bytes(canonical_json_bytes(list(zen.ids))),
        accepted_ids_sha256=sha256_bytes(canonical_json_bytes(list(accepted_ids))),
        decision_sha256=sha256_bytes(canonical_json_bytes(decision_payload)),
        provenance=(zen.provenance, models_dev.provenance, official.provenance),
    )


def _quarantine_reason(
    endpoint: OfficialEndpoint | None,
    model: ModelsDevModel | None,
) -> QuarantineReason | None:
    if endpoint is None or not _transport_is_known(endpoint):
        return "missing_official_transport"
    if model is None or not model.pricing_valid:
        return "invalid_pricing"
    if not model.limits_valid:
        return "invalid_limits"
    return None


def _transport_is_known(endpoint: OfficialEndpoint) -> bool:
    return _transport_key(endpoint) is not None


def _transport(endpoint: OfficialEndpoint) -> TransportName:
    key = _transport_key(endpoint)
    if key is None:
        raise AssertionError("official transport must be classified before use")
    match key:
        case (_OPENAI_ENDPOINT, "@ai-sdk/openai-compatible"):
            return "openai"
        case (_ANTHROPIC_ENDPOINT, "@ai-sdk/anthropic"):
            return "anthropic"
        case unreachable:
            assert_never(unreachable)


def _transport_key(endpoint: OfficialEndpoint) -> KnownTransportKey | None:
    pair = (endpoint.endpoint, endpoint.package)
    if pair == (_OPENAI_ENDPOINT, "@ai-sdk/openai-compatible"):
        return (
            "https://opencode.ai/zen/go/v1/chat/completions",
            "@ai-sdk/openai-compatible",
        )
    if pair == (_ANTHROPIC_ENDPOINT, "@ai-sdk/anthropic"):
        return (
            "https://opencode.ai/zen/go/v1/messages",
            "@ai-sdk/anthropic",
        )
    return None


def _official_pricing_sha256(rows: tuple[OfficialPriceRow, ...]) -> str | None:
    row_hashes = tuple(row.row_sha256 for row in rows)
    if not row_hashes:
        return None
    if len(row_hashes) == 1:
        return row_hashes[0]
    return sha256_bytes(canonical_json_bytes(list(row_hashes)))
