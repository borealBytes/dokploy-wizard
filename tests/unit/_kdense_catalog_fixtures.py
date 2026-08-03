from __future__ import annotations

from dokploy_wizard.dokploy.workspace_catalog_sync import CatalogModel, KdenseCatalogMetadata


def kdense_metadata_for(alias: str) -> KdenseCatalogMetadata:
    return KdenseCatalogMetadata(
        prompt=3.0,
        completion=15.0,
        input_cache_read=0.3,
        input_cache_write=3.75,
        context_length=200000,
        max_completion_tokens=16000,
        source_id=alias.removeprefix("opencode-go/"),
        merged_decision_sha256="a" * 64,
        pricing_selection_sha256="b" * 64,
    )


def catalog_model_for(credential_environment: str) -> CatalogModel:
    alias = "opencode-go/deepseek"
    return CatalogModel(
        alias=alias,
        display_name="DeepSeek",
        kdense_metadata=(
            kdense_metadata_for(alias)
            if credential_environment == "KDENSE_LITELLM_API_KEY"
            else None
        ),
    )
