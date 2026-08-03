from __future__ import annotations

from datetime import UTC, datetime

from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_projection import (
    ModelDecisionProjection,
    ModelsDevModelProjection,
    OfficialModelProjection,
    ZenModelProjection,
)
from dokploy_wizard.litellm.catalog_types import (
    Modalities,
    ModelLimits,
    ResolvedPrice,
    ResolvedPriceTier,
    ResolvedPricing,
    TransportName,
)


def catalog_model(
    *, source_id: str = "minimax-m2.7", transport: TransportName = "openai"
) -> CatalogModel:
    observed_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    pricing = ResolvedPricing(
        tiers=(
            ResolvedPriceTier(
                name="default",
                input=ResolvedPrice(value=0.0000003, provenance="max_both"),
                output=ResolvedPrice(value=0.0000012, provenance="official_only"),
                cache_read=ResolvedPrice(value=0.00000006, provenance="input_fallback"),
                cache_write=ResolvedPrice(value=0.000000375, provenance="models_dev_only"),
            ),
        ),
        scalar_input_per_token=0.0000003,
        scalar_output_per_token=0.0000012,
        scalar_cache_read_per_token=0.00000006,
        scalar_cache_write_per_token=0.000000375,
    )
    projection = ModelDecisionProjection(
        zen=ZenModelProjection(
            url="https://opencode.ai/zen/go/v1/models",
            snapshot_sha256="1" * 64,
            row_sha256="2" * 64,
            observed_at=observed_at,
        ),
        models_dev=ModelsDevModelProjection(
            url="https://models.dev/api.json",
            snapshot_sha256="3" * 64,
            row_sha256="4" * 64,
            pricing_row_sha256="5" * 64,
            provider_npm="@ai-sdk/openai-compatible",
            observed_at=observed_at,
        ),
        official_transport=OfficialModelProjection(
            url="https://raw.example.test/transport.csv",
            commit="0df2f6245a9cd966c0912e12db2c9d809e0c589f",
            blob_sha256="6" * 64,
            snapshot_sha256="7" * 64,
            row_sha256="8" * 64,
        ),
        official_pricing=OfficialModelProjection(
            url="https://raw.example.test/pricing.csv",
            commit="0df2f6245a9cd966c0912e12db2c9d809e0c589f",
            blob_sha256="9" * 64,
            snapshot_sha256="a" * 64,
            row_sha256="b" * 64,
        ),
    )
    return CatalogModel(
        source_id=source_id,
        name="MiniMax M2.7",
        transport=transport,
        endpoint="/v1/chat/completions",
        package="openai",
        pricing=pricing,
        limits=ModelLimits(context=200_000, output=8_192),
        modalities=Modalities(input=("text",), output=("text",)),
        pricing_tiers_sha256="c" * 64,
        pricing_selection_sha256="d" * 64,
        projection=projection,
        merged_decision_sha256="e" * 64,
    )
