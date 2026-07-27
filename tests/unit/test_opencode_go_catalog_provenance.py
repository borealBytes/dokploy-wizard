from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.litellm.catalog_observation import CatalogSources, build_observation
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_types import (
    Modalities,
    ModelLimits,
    ModelsDevModel,
    PriceDimensions,
)
from tests.unit._opencode_go_catalog_support import StaticClock, catalog_sources

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _accepted_model(sources: CatalogSources) -> CatalogModel:
    observation = build_observation(sources, StaticClock())
    return observation.accepted_models[0]


def _replace_models_dev_model(
    sources: CatalogSources,
    replacement: ModelsDevModel,
) -> CatalogSources:
    models = tuple(
        replacement if model.source_id == replacement.source_id else model
        for model in sources.models_dev.models
    )
    return replace(sources, models_dev=replace(sources.models_dev, models=models))


def test_model_retains_exact_task3_source_projection_and_observation_time() -> None:
    clock = StaticClock()
    sources = catalog_sources(clock)

    observation = build_observation(sources, clock)
    model = observation.accepted_models[0]
    projection = model.projection

    assert observation.observed_at == clock.value
    assert tuple(item.observed_at for item in observation.provenance) == (
        clock.value,
        clock.value,
        clock.value,
    )
    assert projection.zen.url == sources.zen.provenance.url
    assert projection.zen.snapshot_sha256 == sources.zen.provenance.projected_sha256
    assert projection.zen.observed_at == clock.value
    assert projection.models_dev.url == sources.models_dev.provenance.url
    assert projection.models_dev.snapshot_sha256 == sources.models_dev.provenance.projected_sha256
    assert projection.models_dev.provider_npm == sources.models_dev.provider_npm
    assert projection.models_dev.pricing_row_sha256
    assert projection.models_dev.observed_at == clock.value
    assert projection.official_transport.row_sha256
    assert projection.official_pricing.row_sha256
    assert projection.official_transport.url == sources.official.provenance.url
    assert projection.official_pricing.url == sources.official.provenance.url
    assert projection.official_transport.commit == sources.official.provenance.commit
    assert projection.official_pricing.commit == sources.official.provenance.commit
    assert projection.official_transport.blob_sha256 == sources.official.provenance.blob
    assert projection.official_pricing.blob_sha256 == sources.official.provenance.blob
    assert projection.official_transport.snapshot_sha256 == (
        sources.official.provenance.projected_sha256
    )
    assert projection.official_pricing.snapshot_sha256 == (
        sources.official.provenance.projected_sha256
    )
    assert model.pricing_tiers_sha256
    assert model.pricing_selection_sha256
    assert model.merged_decision_sha256


def test_model_decision_hash_changes_for_each_owned_projection() -> None:
    sources = catalog_sources()
    baseline = _accepted_model(sources)
    source_id = baseline.source_id
    model = sources.models_dev.model_for(source_id)
    endpoint = next(row for row in sources.official.endpoints if row.source_id == source_id)
    assert model is not None
    assert model.pricing is not None
    assert model.limits is not None
    assert model.pricing.base.input is not None

    transport_endpoint = (
        replace(
            endpoint,
            endpoint="https://opencode.ai/zen/go/v1/messages",
            package="@ai-sdk/anthropic",
        )
        if baseline.transport == "openai"
        else replace(
            endpoint,
            endpoint="https://opencode.ai/zen/go/v1/chat/completions",
            package="@ai-sdk/openai-compatible",
        )
    )
    transport_sources = replace(
        sources,
        official=replace(
            sources.official,
            endpoints=tuple(
                transport_endpoint if row.source_id == source_id else row
                for row in sources.official.endpoints
            ),
        ),
    )
    row_sources = _replace_models_dev_model(
        sources,
        replace(model, name=f"{model.name} changed", projected_row_sha256=_DIGEST_A),
    )
    pricing_sources = _replace_models_dev_model(
        sources,
        replace(
            model,
            pricing=replace(
                model.pricing,
                base=replace(model.pricing.base, input=model.pricing.base.input + 1.0),
            ),
            pricing_row_sha256=_DIGEST_B,
        ),
    )
    limits_sources = _replace_models_dev_model(
        sources,
        replace(model, limits=ModelLimits(model.limits.context + 1, model.limits.output)),
    )
    modality_sources = _replace_models_dev_model(
        sources,
        replace(model, modalities=Modalities(("image", "text"), ("text",))),
    )
    snapshot_sources = replace(
        sources,
        models_dev=replace(
            sources.models_dev,
            provenance=replace(sources.models_dev.provenance, projected_sha256=_DIGEST_A),
        ),
    )
    official_snapshot_sources = replace(
        sources,
        official=replace(
            sources.official,
            provenance=replace(sources.official.provenance, projected_sha256=_DIGEST_B),
        ),
    )
    zen_row_sources = replace(
        sources,
        zen=replace(
            sources.zen,
            row_sha256_by_id=tuple(
                (model_id, _DIGEST_B if model_id == source_id else digest)
                for model_id, digest in sources.zen.row_sha256_by_id
            ),
        ),
    )
    official_price = next(
        row
        for row in sources.official.pricing_rows
        if row.base_model_name.lower() in endpoint.model_name.lower()
    )
    official_pricing_sources = replace(
        sources,
        official=replace(
            sources.official,
            pricing_rows=tuple(
                replace(
                    row,
                    dimensions=PriceDimensions(99.0, 99.0, 99.0, 99.0),
                    row_sha256=_DIGEST_B,
                )
                if row == official_price
                else row
                for row in sources.official.pricing_rows
            ),
        ),
    )

    variants = (
        transport_sources,
        row_sources,
        pricing_sources,
        limits_sources,
        modality_sources,
        snapshot_sources,
        official_snapshot_sources,
        zen_row_sources,
        official_pricing_sources,
    )
    assert all(
        _accepted_model(variant).merged_decision_sha256 != baseline.merged_decision_sha256
        for variant in variants
    )


def test_models_dev_provider_npm_is_provenance_not_transport_authority() -> None:
    sources = catalog_sources()
    baseline = _accepted_model(sources)
    changed = replace(
        sources,
        models_dev=replace(sources.models_dev, provider_npm="@ai-sdk/anthropic"),
    )

    observed = _accepted_model(changed)

    assert observed.transport == baseline.transport
    assert observed.projection.models_dev.provider_npm == "@ai-sdk/anthropic"
    assert observed.merged_decision_sha256 != baseline.merged_decision_sha256


def test_model_decision_hash_is_per_model_not_one_shared_source_hash() -> None:
    observation = build_observation(catalog_sources(), StaticClock())

    hashes = tuple(model.merged_decision_sha256 for model in observation.accepted_models)

    assert len(hashes) == len(set(hashes))
