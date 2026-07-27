from __future__ import annotations

from dokploy_wizard.litellm.catalog_observation import CatalogSources, build_observation
from dokploy_wizard.litellm.catalog_pricing import merge_pricing
from dokploy_wizard.litellm.catalog_types import (
    PriceDimensions,
    PriceTier,
    SourcePricing,
)
from tests.unit._opencode_go_catalog_support import (
    StaticClock,
    catalog_sources,
    mixed_fixture,
)


def test_current_mixed_catalog_accepts_16_and_quarantines_exact_six() -> None:
    sources = catalog_sources()

    observation = build_observation(sources, StaticClock())

    fixture = mixed_fixture()
    assert observation.status == "accepted_with_quarantine"
    assert observation.accepted_ids == tuple(fixture["valid_ids"])
    assert tuple(record.source_id for record in observation.quarantine) == tuple(
        fixture["quarantined_ids"]
    )
    assert {model.transport for model in observation.accepted_models} == {"anthropic", "openai"}
    assert observation.source_ids == tuple(fixture["zen_ids"])


def test_present_unclassifiable_preserves_lkg_but_new_id_is_hidden() -> None:
    sources = catalog_sources()
    initial = build_observation(sources, StaticClock())
    preserved = initial.accepted_models[0].with_source_id("kimi-k2.5")

    observation = build_observation(
        CatalogSources(sources.zen, sources.models_dev, sources.official),
        StaticClock(),
        previous_visible=(preserved,),
    )

    assert tuple(model.source_id for model in observation.preserved_models) == ("kimi-k2.5",)
    assert "hy3-preview" not in tuple(model.source_id for model in observation.visible_models)


def test_tiered_pricing_takes_conservative_max_and_input_fallback() -> None:
    official = SourcePricing(
        base=PriceDimensions(0.3, 1.2, 0.06, 0.375),
        tiers=(),
    )
    models_dev = SourcePricing(
        base=PriceDimensions(0.4, 1.0, None, None),
        tiers=(PriceTier("large", PriceDimensions(0.5, 1.5, 0.1, None)),),
    )

    merged = merge_pricing(official, models_dev)

    assert merged.scalar_input_per_token == 0.5 / 1_000_000
    assert merged.scalar_output_per_token == 1.5 / 1_000_000
    large = next(tier for tier in merged.tiers if tier.name == "large")
    assert large.cache_read.value == 0.1
    assert large.cache_read.provenance == "max_both"
    assert large.cache_write.value == 0.375
    assert large.cache_write.provenance == "official_only"


def test_pricing_cache_falls_back_to_selected_explicit_zero_input() -> None:
    merged = merge_pricing(SourcePricing(PriceDimensions(0.0, 1.0, None, None), ()), None)

    assert merged.tiers[0].cache_read.value == 0.0
    assert merged.tiers[0].cache_read.provenance == "input_fallback"
    assert merged.tiers[0].cache_write.value == 0.0
