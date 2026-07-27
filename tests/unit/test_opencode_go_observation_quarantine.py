from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.litellm.catalog_observation import CatalogSources, build_observation
from dokploy_wizard.litellm.catalog_types import (
    ModelsDevModel,
    PriceDimensions,
    SourcePricing,
)
from tests.unit._opencode_go_catalog_support import StaticClock, catalog_sources


def _sources_with_model(
    sources: CatalogSources,
    replacement: ModelsDevModel,
) -> CatalogSources:
    return replace(
        sources,
        models_dev=replace(
            sources.models_dev,
            models=tuple(
                replacement if model.source_id == replacement.source_id else model
                for model in sources.models_dev.models
            ),
        ),
    )


def _source_model(sources: CatalogSources) -> ModelsDevModel:
    source_id = sources.zen.ids[0]
    model = sources.models_dev.model_for(source_id)
    assert model is not None
    return model


def test_missing_official_transport_binds_official_snapshot_evidence() -> None:
    sources = catalog_sources()
    model = _source_model(sources)
    without_endpoint = replace(
        sources,
        official=replace(
            sources.official,
            endpoints=tuple(
                endpoint
                for endpoint in sources.official.endpoints
                if endpoint.source_id != model.source_id
            ),
        ),
    )

    observation = build_observation(without_endpoint, StaticClock())
    quarantine = next(item for item in observation.quarantine if item.source_id == model.source_id)

    assert quarantine.reason == "missing_official_transport"
    assert quarantine.source_sha256 == sources.official.provenance.projected_sha256


def test_invalid_models_dev_pricing_binds_pricing_row_evidence() -> None:
    sources = catalog_sources()
    model = _source_model(sources)
    invalid = _sources_with_model(
        sources,
        replace(model, pricing=None, pricing_valid=False),
    )

    observation = build_observation(invalid, StaticClock())
    quarantine = next(item for item in observation.quarantine if item.source_id == model.source_id)

    assert quarantine.reason == "invalid_pricing"
    assert quarantine.source_sha256 == model.pricing_row_sha256


def test_absent_models_dev_row_binds_models_dev_snapshot_evidence() -> None:
    sources = catalog_sources()
    model = _source_model(sources)
    without_model = replace(
        sources,
        models_dev=replace(
            sources.models_dev,
            models=tuple(
                item for item in sources.models_dev.models if item.source_id != model.source_id
            ),
        ),
    )

    observation = build_observation(without_model, StaticClock())
    quarantine = next(item for item in observation.quarantine if item.source_id == model.source_id)

    assert quarantine.reason == "invalid_pricing"
    assert quarantine.source_sha256 == sources.models_dev.provenance.projected_sha256


def test_invalid_models_dev_limits_bind_projected_row_evidence() -> None:
    sources = catalog_sources()
    model = _source_model(sources)
    invalid = _sources_with_model(
        sources,
        replace(model, limits=None, limits_valid=False),
    )

    observation = build_observation(invalid, StaticClock())
    quarantine = next(item for item in observation.quarantine if item.source_id == model.source_id)

    assert quarantine.reason == "invalid_limits"
    assert quarantine.source_sha256 == model.projected_row_sha256


def test_pricing_merge_failure_preserves_visible_model_and_models_dev_evidence() -> None:
    sources = catalog_sources()
    baseline = build_observation(sources, StaticClock())
    previous_visible = baseline.accepted_models[0]
    model = sources.models_dev.model_for(previous_visible.source_id)
    endpoint = next(
        item
        for item in sources.official.endpoints
        if item.source_id == previous_visible.source_id
    )
    assert model is not None
    invalid = _sources_with_model(
        sources,
        replace(
            model,
            pricing=SourcePricing(PriceDimensions(None, None, None, None), ()),
        ),
    )
    without_pricing = replace(
        invalid,
        official=replace(
            invalid.official,
            pricing_rows=tuple(
                row
                for row in invalid.official.pricing_rows
                if row.base_model_name.lower() not in endpoint.model_name.lower()
            ),
        ),
    )

    observation = build_observation(
        without_pricing,
        StaticClock(),
        previous_visible=(previous_visible,),
    )
    quarantine = next(
        item for item in observation.quarantine if item.source_id == previous_visible.source_id
    )

    assert observation.preserved_models == (previous_visible,)
    assert quarantine.reason == "invalid_pricing"
    assert quarantine.source_sha256 == model.pricing_row_sha256
