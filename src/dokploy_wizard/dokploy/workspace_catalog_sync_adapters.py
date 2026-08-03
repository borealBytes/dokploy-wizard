"""Owned-pointer plans for supported Coder workspace model adapters."""

from __future__ import annotations

import os
import stat
from math import isfinite
from pathlib import Path

from dokploy_wizard.dokploy.workspace_catalog_sync_contracts import (
    require_internal_base_url,
    require_sha256,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_io import sha256
from dokploy_wizard.dokploy.workspace_catalog_sync_json import (
    compact_json,
    json_document_sha,
    patch_json_pointer,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    AdapterPlan,
    CatalogTarget,
    JsonValue,
    KdenseCatalogMetadata,
    ModelCatalog,
    OwnedPointer,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_yaml import (
    patch_yaml_pointer,
    yaml_pointer_exists,
)


def adapter_plan(adapter: str, workspace_root: Path, catalog: ModelCatalog) -> AdapterPlan:
    plans = {
        "primary": (_primary_plan, "OPENAI_API_KEY"),
        "opencode-web": (_opencode_web_plan, "OPENAI_API_KEY"),
        "hermes": (_hermes_plan, "OPENAI_API_KEY"),
        "kdense": (_kdense_plan, "KDENSE_LITELLM_API_KEY"),
    }
    try:
        render, credential_environment = plans[adapter]
    except KeyError as error:
        raise WorkspaceCatalogSyncError("workspace adapter is unsupported") from error
    _validate_catalog(catalog, credential_environment, require_kdense=adapter == "kdense")
    return render(workspace_root, catalog)


def _primary_plan(root: Path, catalog: ModelCatalog) -> AdapterPlan:
    return AdapterPlan(
        targets=_opencode_targets(root, catalog, include_pi=True),
        health_endpoints=("/v1/models",),
        credential_environment="OPENAI_API_KEY",
        base_environment="OPENAI_API_BASE",
    )


def _opencode_web_plan(root: Path, catalog: ModelCatalog) -> AdapterPlan:
    return AdapterPlan(
        targets=_opencode_targets(root, catalog, include_pi=False),
        health_endpoints=("/v1/models",),
        credential_environment="OPENAI_API_KEY",
        base_environment="OPENAI_API_BASE",
    )


def _hermes_plan(root: Path, catalog: ModelCatalog) -> AdapterPlan:
    path = root / ".hermes" / "config.yaml"
    content = path.read_bytes() if path.exists() else b""
    provider: JsonValue = {
        "base_url": catalog.base_url,
        "key_env": "OPENAI_API_KEY",
        "discover_models": False,
        "models": [model.alias for model in catalog.models],
    }
    routes: JsonValue = {model.alias: "openai" for model in catalog.models}
    provider_patch = patch_yaml_pointer(content, ("providers", "openai"), provider)
    routes_patch = patch_yaml_pointer(
        provider_patch.content,
        ("platforms", "api_server", "extra", "model_routes"),
        routes,
    )
    rendered = routes_patch.content
    model_pointers = []
    if not yaml_pointer_exists(rendered, ("model", "provider")):
        provider_model_patch = patch_yaml_pointer(
            rendered, ("model", "provider"), "openai"
        )
        rendered = provider_model_patch.content
        model_pointers.append(
            OwnedPointer(
                pointer="/model/provider",
                pre_sha256=provider_model_patch.pre_sha256,
                post_sha256=provider_model_patch.post_sha256,
            )
        )
    if not yaml_pointer_exists(rendered, ("model", "default")):
        default_model_patch = patch_yaml_pointer(
            rendered,
            ("model", "default"),
            catalog.models[0].alias,
        )
        rendered = default_model_patch.content
        model_pointers.append(
            OwnedPointer(
                pointer="/model/default",
                pre_sha256=default_model_patch.pre_sha256,
                post_sha256=default_model_patch.post_sha256,
            )
        )
    target = CatalogTarget(
        path=path,
        kind="file",
        content=rendered,
        mode=0o600,
        owned_pointers=(
            OwnedPointer(
                pointer="/providers/openai",
                pre_sha256=provider_patch.pre_sha256,
                post_sha256=provider_patch.post_sha256,
            ),
            OwnedPointer(
                pointer="/platforms/api_server/extra/model_routes",
                pre_sha256=routes_patch.pre_sha256,
                post_sha256=routes_patch.post_sha256,
            ),
            *model_pointers,
        ),
    )
    return AdapterPlan(
        targets=(target,),
        health_endpoints=("http://127.0.0.1:8642/health", "http://127.0.0.1:9119/api/status"),
        credential_environment="OPENAI_API_KEY",
        base_environment="OPENAI_API_BASE",
    )


def _kdense_plan(root: Path, catalog: ModelCatalog) -> AdapterPlan:
    state_root = root / ".local" / "state" / "dokploy-wizard" / "model-sync"
    models: JsonValue = [
        _kdense_model(model.alias, model.display_name, model.kdense_metadata)
        for model in catalog.models
    ]
    content = compact_json(models) + b"\n"
    generation = sha256(content)[:16]
    generation_dir = state_root / "generations" / generation
    generated = generation_dir / "models.json"
    current = state_root / "current"
    current_target = f"generations/{generation}"
    file_pre = json_document_sha(generated.read_bytes()) if generated.exists() else None
    link_pre = _symlink_sha(current)
    targets = (
        CatalogTarget(
            path=generated,
            kind="file",
            content=content,
            mode=0o600,
            owned_pointers=(
                OwnedPointer(
                    pointer="/",
                    pre_sha256=file_pre,
                    post_sha256=json_document_sha(content),
                ),
            ),
        ),
        CatalogTarget(
            path=current,
            kind="symlink",
            content=current_target.encode(),
            mode=0,
            owned_pointers=(
                OwnedPointer(
                    pointer="/",
                    pre_sha256=link_pre,
                    post_sha256=sha256(current_target.encode()),
                ),
            ),
        ),
    )
    return AdapterPlan(
        targets=targets,
        health_endpoints=("http://127.0.0.1:8000/health", "http://127.0.0.1:3001/health"),
        credential_environment="KDENSE_LITELLM_API_KEY",
        base_environment="KDENSE_LITELLM_BASE_URL",
    )


def _opencode_targets(
    root: Path, catalog: ModelCatalog, *, include_pi: bool
) -> tuple[CatalogTarget, ...]:
    targets = [
        _json_target(
            root / ".config/opencode/opencode.json",
            "/provider/litellm",
            {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": catalog.base_url, "apiKey": "${OPENAI_API_KEY}"},
                "models": {model.alias: {} for model in catalog.models},
            },
        ),
        _copilot_target(root / ".local/share/code-server/User/settings.json", catalog),
        _copilot_target(root / ".config/code-server/User/settings.json", catalog),
    ]
    if include_pi:
        targets.append(
            _json_target(
                root / ".pi/agent/models.json",
                "/providers/litellm",
                {
                    "baseUrl": catalog.base_url,
                    "api": "openai-completions",
                    "apiKey": "${OPENAI_API_KEY}",
                    "models": [
                        {"id": model.alias, "name": model.display_name} for model in catalog.models
                    ],
                },
            )
        )
    return tuple(targets)


def _copilot_target(path: Path, catalog: ModelCatalog) -> CatalogTarget:
    return _json_target(
        path,
        "/github.copilot.chat.customOAIModels",
        {
            model.alias: {
                "name": model.display_name,
                "url": catalog.base_url,
                "apiKey": "${OPENAI_API_KEY}",
            }
            for model in catalog.models
        },
    )


def _json_target(path: Path, pointer: str, value: JsonValue) -> CatalogTarget:
    from dokploy_wizard.dokploy.workspace_catalog_sync_json import parse_json_pointer

    content = path.read_bytes() if path.exists() else b"{}\n"
    patch = patch_json_pointer(content, parse_json_pointer(pointer), value)
    return CatalogTarget(
        path=path,
        kind="file",
        content=patch.content,
        mode=0o600,
        owned_pointers=(
            OwnedPointer(
                pointer=pointer,
                pre_sha256=patch.pre_sha256,
                post_sha256=patch.post_sha256,
            ),
        ),
    )


def _validate_catalog(
    catalog: ModelCatalog, credential_environment: str, *, require_kdense: bool
) -> None:
    require_internal_base_url(catalog.base_url)
    require_sha256(catalog.credential_value_sha256, "workspace catalog credential hash")
    if catalog.credential_environment != credential_environment:
        raise WorkspaceCatalogSyncError("workspace catalog credential environment is invalid")
    aliases = [model.alias for model in catalog.models]
    if (
        not aliases
        or len(set(aliases)) != len(aliases)
        or any(not model.alias or not model.display_name for model in catalog.models)
    ):
        raise WorkspaceCatalogSyncError("workspace catalog aliases are invalid")
    if require_kdense:
        for model in catalog.models:
            _validate_kdense_model(model.alias, model.kdense_metadata)


def _kdense_model(
    alias: str, display_name: str, metadata: KdenseCatalogMetadata | None
) -> JsonValue:
    _validate_kdense_model(alias, metadata)
    if metadata is None:
        raise WorkspaceCatalogSyncError("K-Dense catalog metadata is unavailable")
    return {
        "id": alias,
        "label": display_name,
        "provider": "LiteLLM",
        "context_length": metadata.context_length,
        "max_completion_tokens": metadata.max_completion_tokens,
        "pricing": {
            "prompt": metadata.prompt,
            "completion": metadata.completion,
            "input_cache_read": metadata.input_cache_read,
            "input_cache_write": metadata.input_cache_write,
        },
        "provenance": {
            "managed_by": "dokploy-wizard",
            "managed_catalog": "opencode-go",
            "source_id": metadata.source_id,
            "merged_decision_sha256": metadata.merged_decision_sha256,
            "dokploy_pricing_selection_sha256": metadata.pricing_selection_sha256,
        },
    }


def _validate_kdense_model(alias: str, metadata: KdenseCatalogMetadata | None) -> None:
    if metadata is None:
        raise WorkspaceCatalogSyncError("K-Dense catalog metadata is unavailable")
    source_id = alias.removeprefix("opencode-go/")
    if not alias.startswith("opencode-go/") or not source_id:
        raise WorkspaceCatalogSyncError("K-Dense catalog alias is invalid")
    if metadata.source_id != source_id:
        raise WorkspaceCatalogSyncError("K-Dense catalog provenance is invalid")
    if (
        not isfinite(metadata.prompt)
        or not isfinite(metadata.completion)
        or metadata.prompt <= 0
        or metadata.completion <= 0
        or not isfinite(metadata.input_cache_read)
        or not isfinite(metadata.input_cache_write)
        or metadata.input_cache_read < 0
        or metadata.input_cache_write < 0
    ):
        raise WorkspaceCatalogSyncError("K-Dense catalog pricing is invalid")
    if metadata.context_length <= 0 or metadata.max_completion_tokens <= 0:
        raise WorkspaceCatalogSyncError("K-Dense catalog limits are invalid")
    if not _is_sha256(metadata.merged_decision_sha256) or not _is_sha256(
        metadata.pricing_selection_sha256
    ):
        raise WorkspaceCatalogSyncError("K-Dense catalog provenance is invalid")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _symlink_sha(path: Path) -> str | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceCatalogSyncError("workspace K-Dense current target is not a symlink")
    return sha256(os.readlink(path).encode())
