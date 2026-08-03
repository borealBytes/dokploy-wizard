from __future__ import annotations

import json
from pathlib import Path

from dokploy_wizard.dokploy.workspace_catalog_sync import CatalogModel, ModelCatalog
from dokploy_wizard.dokploy.workspace_catalog_sync_models import JsonValue
from dokploy_wizard.dokploy.workspace_catalog_sync_runtime_inputs import ModelSyncSettings


def catalog(*aliases: str) -> ModelCatalog:
    return ModelCatalog(
        base_url="http://wizard-shared-litellm:4000/v1",
        credential_environment="OPENAI_API_KEY",
        credential_value_sha256="a" * 64,
        models=tuple(CatalogModel(alias=alias, display_name=alias) for alias in aliases),
    )


def settings() -> ModelSyncSettings:
    return ModelSyncSettings(
        base_url="http://wizard-shared-litellm:4000/v1",
        api_key="test-key",
        default_alias="opencode-go/default",
        fallback_aliases=("openrouter/fallback",),
    )


def opencode_models(root: Path) -> dict[str, JsonValue]:
    payload: JsonValue = json.loads(
        (root / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    provider = _json_mapping(_json_mapping(payload)["provider"])
    return _json_mapping(_json_mapping(provider["litellm"])["models"])


def _json_mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("expected JSON mapping")
    return value
