from __future__ import annotations

import math

from dokploy_wizard.litellm.catalog_types import (
    PriceDimensions,
    PriceProvenance,
    ResolvedPrice,
    ResolvedPriceTier,
    ResolvedPricing,
    SourcePricing,
)


class PricingContractError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


def merge_pricing(
    official: SourcePricing | None,
    models_dev: SourcePricing | None,
) -> ResolvedPricing:
    if official is None and models_dev is None:
        raise PricingContractError("pricing is absent from both sources")
    tier_names = {"base"}
    tier_names.update(
        tier.name
        for source in (official, models_dev)
        if source
        for tier in source.tiers
    )
    tiers = tuple(
        _resolve_tier(name, official, models_dev)
        for name in ("base", *sorted(tier_names - {"base"}))
    )
    return ResolvedPricing(
        tiers=tiers,
        scalar_input_per_token=max(tier.input.value for tier in tiers) / 1_000_000,
        scalar_output_per_token=max(tier.output.value for tier in tiers) / 1_000_000,
        scalar_cache_read_per_token=max(tier.cache_read.value for tier in tiers) / 1_000_000,
        scalar_cache_write_per_token=max(tier.cache_write.value for tier in tiers) / 1_000_000,
    )


def pricing_is_valid(pricing: SourcePricing | None) -> bool:
    if pricing is None:
        return False
    dimensions = (pricing.base, *(tier.dimensions for tier in pricing.tiers))
    return all(
        item.input is not None
        and item.output is not None
        and _valid_value(item.input)
        and _valid_value(item.output)
        and (item.cache_read is None or _valid_value(item.cache_read))
        and (item.cache_write is None or _valid_value(item.cache_write))
        for item in dimensions
    )


def _resolve_tier(
    name: str,
    official: SourcePricing | None,
    models_dev: SourcePricing | None,
) -> ResolvedPriceTier:
    official_values = _effective_dimensions(official, name)
    models_values = _effective_dimensions(models_dev, name)
    selected_input = _required_price(official_values.input, models_values.input, "input")
    selected_output = _required_price(official_values.output, models_values.output, "output")
    return ResolvedPriceTier(
        name=name,
        input=selected_input,
        output=selected_output,
        cache_read=_cache_price(
            official_values.cache_read,
            models_values.cache_read,
            selected_input.value,
        ),
        cache_write=_cache_price(
            official_values.cache_write,
            models_values.cache_write,
            selected_input.value,
        ),
    )


def _effective_dimensions(source: SourcePricing | None, tier_name: str) -> PriceDimensions:
    if source is None:
        return PriceDimensions(None, None, None, None)
    if tier_name == "base":
        return source.base
    tier = next((item for item in source.tiers if item.name == tier_name), None)
    if tier is None:
        return source.base
    return PriceDimensions(
        tier.dimensions.input if tier.dimensions.input is not None else source.base.input,
        tier.dimensions.output if tier.dimensions.output is not None else source.base.output,
        tier.dimensions.cache_read
        if tier.dimensions.cache_read is not None
        else source.base.cache_read,
        tier.dimensions.cache_write
        if tier.dimensions.cache_write is not None
        else source.base.cache_write,
    )


def _required_price(
    official: float | None,
    models_dev: float | None,
    dimension: str,
) -> ResolvedPrice:
    if official is None and models_dev is None:
        raise PricingContractError(f"{dimension} price is absent from both sources")
    value, provenance = _max_explicit(official, models_dev)
    return ResolvedPrice(value, provenance)


def _cache_price(
    official: float | None,
    models_dev: float | None,
    selected_input: float,
) -> ResolvedPrice:
    if official is None and models_dev is None:
        return ResolvedPrice(selected_input, "input_fallback")
    value, provenance = _max_explicit(official, models_dev)
    return ResolvedPrice(value, provenance)


def _max_explicit(
    official: float | None,
    models_dev: float | None,
) -> tuple[float, PriceProvenance]:
    if official is not None and models_dev is not None:
        return max(official, models_dev), "max_both"
    if official is not None:
        return official, "official_only"
    if models_dev is not None:
        return models_dev, "models_dev_only"
    raise PricingContractError("explicit price is absent")


def _valid_value(value: float) -> bool:
    return math.isfinite(value) and value >= 0
