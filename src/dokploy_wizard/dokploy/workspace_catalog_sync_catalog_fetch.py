"""HTTP parsing for generic and K-Dense central LiteLLM catalogs."""

from __future__ import annotations

import json
from typing import Final, assert_never
from urllib.error import URLError
from urllib.request import Request, urlopen

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogModel,
    JsonValue,
    KdenseCatalogMetadata,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_runtime_inputs import (
    ModelSyncSettings,
    is_model_alias,
    unique_aliases,
)

_MAX_CATALOG_BYTES: Final = 2 * 1024 * 1024


class CatalogUnavailableError(WorkspaceCatalogSyncError):
    pass


def fetch_model_aliases(settings: ModelSyncSettings) -> tuple[str, ...]:
    payload = _fetch_models_payload(settings)
    match payload:
        case {"data": list() as entries}:
            aliases = tuple(_model_alias(entry) for entry in entries)
        case None | str() | int() | float() | list() | dict():
            raise WorkspaceCatalogSyncError("LiteLLM catalog is invalid")
        case unreachable:
            assert_never(unreachable)
    if not aliases:
        raise WorkspaceCatalogSyncError("LiteLLM catalog is empty")
    return unique_aliases(aliases)


def fetch_kdense_models(settings: ModelSyncSettings) -> tuple[CatalogModel, ...]:
    payload = _fetch_models_payload(settings)
    match payload:
        case {"data": list() as entries}:
            models = tuple(
                _kdense_model_from_entry(entry) for entry in entries if _is_kdense_candidate(entry)
            )
        case None | str() | int() | float() | list() | dict():
            raise WorkspaceCatalogSyncError("LiteLLM catalog is invalid")
        case unreachable:
            assert_never(unreachable)
    if not models:
        raise WorkspaceCatalogSyncError("K-Dense catalog is empty")
    if len({model.alias for model in models}) != len(models):
        raise WorkspaceCatalogSyncError("K-Dense catalog aliases are invalid")
    return models


def _fetch_models_payload(settings: ModelSyncSettings) -> JsonValue:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {settings.api_key}"}
    request = Request(f"{settings.base_url}/models", headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read(_MAX_CATALOG_BYTES + 1)
    except (OSError, URLError) as error:
        raise CatalogUnavailableError("LiteLLM catalog is unavailable") from error
    if len(raw) > _MAX_CATALOG_BYTES:
        raise WorkspaceCatalogSyncError("LiteLLM catalog is oversized")
    try:
        payload: JsonValue = json.loads(raw)
        return payload
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceCatalogSyncError("LiteLLM catalog is invalid") from error


def _is_kdense_candidate(entry: JsonValue) -> bool:
    match entry:
        case {"id": str() as alias}:
            return alias.startswith("opencode-go/")
        case None | str() | int() | float() | list() | dict():
            return False
        case unreachable:
            assert_never(unreachable)


def _kdense_model_from_entry(entry: JsonValue) -> CatalogModel:
    document = _json_mapping(entry)
    alias = document.get("id")
    if not isinstance(alias, str):
        raise WorkspaceCatalogSyncError("K-Dense catalog model is invalid")
    model_info = _json_mapping(document.get("model_info", document))
    return CatalogModel(
        alias=alias,
        display_name=alias,
        kdense_metadata=_kdense_metadata(alias, model_info),
    )


def _json_mapping(value: JsonValue) -> dict[str, JsonValue]:
    match value:
        case dict() as document:
            return {str(key): item for key, item in document.items()}
        case None | str() | int() | float() | list():
            raise WorkspaceCatalogSyncError("K-Dense catalog model is invalid")
        case unreachable:
            assert_never(unreachable)


def _kdense_metadata(alias: str, model_info: dict[str, JsonValue]) -> KdenseCatalogMetadata:
    return KdenseCatalogMetadata(
        prompt=_required_price(model_info, "dokploy_scalar_input_per_million"),
        completion=_required_price(model_info, "dokploy_scalar_output_per_million"),
        input_cache_read=_required_price(model_info, "dokploy_scalar_cache_read_per_million"),
        input_cache_write=_required_price(model_info, "dokploy_scalar_cache_write_per_million"),
        context_length=_required_positive_integer(model_info, "max_input_tokens"),
        max_completion_tokens=_required_positive_integer(model_info, "max_output_tokens"),
        source_id=_required_text(model_info, "source_id"),
        merged_decision_sha256=_required_text(model_info, "merged_decision_sha256"),
        pricing_selection_sha256=_required_text(model_info, "dokploy_pricing_selection_sha256"),
    )


def _required_text(document: dict[str, JsonValue], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise WorkspaceCatalogSyncError("K-Dense catalog provenance is invalid")
    return value


def _required_price(document: dict[str, JsonValue], name: str) -> float:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise WorkspaceCatalogSyncError("K-Dense catalog pricing is invalid")
    return float(value)


def _required_positive_integer(document: dict[str, JsonValue], name: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkspaceCatalogSyncError("K-Dense catalog limits are invalid")
    return value


def _model_alias(value: JsonValue) -> str:
    match value:
        case {"id": str() as alias} if is_model_alias(alias):
            return alias
        case None | str() | int() | float() | list() | dict():
            raise WorkspaceCatalogSyncError("LiteLLM catalog model alias is invalid")
        case unreachable:
            assert_never(unreachable)
