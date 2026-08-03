from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Final, Literal, assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_adapters import adapter_plan
from dokploy_wizard.dokploy.workspace_catalog_sync_catalog_fetch import (
    CatalogUnavailableError,
    fetch_kdense_models,
    fetch_model_aliases,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_hermes import (
    HermesProcessReload,
    refresh_hermes_catalog,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_io import ensure_private_directory
from dokploy_wizard.dokploy.workspace_catalog_sync_kdense import (
    KdenseProcessReload,
    refresh_kdense_catalog,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogModel,
    ModelCatalog,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_record_lock import RecordLock
from dokploy_wizard.dokploy.workspace_catalog_sync_runtime_inputs import (
    ModelSyncSettings,
    is_model_alias,
    kdense_settings_from_environment,
    parse_runtime_command,
    settings_from_environment,
    unique_aliases,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_transaction import WorkspaceCatalogTransaction

_MAX_CATALOG_BYTES: Final = 2 * 1024 * 1024
_STATE_RELATIVE_PATH: Final = Path(".local/state/dokploy-wizard/model-sync")

FileFreshness = Literal["updated", "current", "fallback", "retained"]


@dataclass(frozen=True, slots=True)
class ModelSyncRefresh:
    file_freshness: FileFreshness
    process_freshness: Literal["new-process-required", "reloaded", "not-required"]


def refresh_from_environment(
    *,
    adapter: str,
    workspace_root: Path,
    hermes_process_reload: HermesProcessReload | None = None,
    kdense_process_reload: KdenseProcessReload | None = None,
) -> ModelSyncRefresh:
    settings = (
        kdense_settings_from_environment(os.environ)
        if adapter == "kdense"
        else settings_from_environment(os.environ)
    )
    return refresh_from_settings(
        workspace_root=workspace_root,
        adapter=adapter,
        settings=settings,
        hermes_process_reload=hermes_process_reload,
        kdense_process_reload=kdense_process_reload,
    )


def refresh_from_settings(
    *,
    workspace_root: Path,
    adapter: str,
    settings: ModelSyncSettings,
    fetcher: Callable[[ModelSyncSettings], tuple[str, ...]] | None = None,
    hermes_process_reload: HermesProcessReload | None = None,
    kdense_process_reload: KdenseProcessReload | None = None,
) -> ModelSyncRefresh:
    if adapter == "kdense":
        return _refresh_kdense_from_settings(
            workspace_root=workspace_root,
            settings=settings,
            process_reload=kdense_process_reload,
        )
    resolved_fetcher = fetch_model_aliases if fetcher is None else fetcher
    try:
        aliases = resolved_fetcher(settings)
    except CatalogUnavailableError:
        root = workspace_root.resolve(strict=True)
        with _refresh_lock(root):
            fallback = _catalog(settings, settings.fallback_aliases)
            plan = adapter_plan(adapter, root, fallback)
            if any(target.path.exists() or target.path.is_symlink() for target in plan.targets):
                return ModelSyncRefresh("retained", "new-process-required")
            return _apply_locked(root, adapter, fallback, "fallback", hermes_process_reload, None)
    return refresh_catalog(
        workspace_root=workspace_root,
        adapter=adapter,
        catalog=_catalog(settings, aliases),
        hermes_process_reload=hermes_process_reload,
    )


def _refresh_kdense_from_settings(
    *,
    workspace_root: Path,
    settings: ModelSyncSettings,
    process_reload: KdenseProcessReload | None,
) -> ModelSyncRefresh:
    try:
        models = fetch_kdense_models(settings)
    except CatalogUnavailableError:
        root = workspace_root.resolve(strict=True)
        current = root / _STATE_RELATIVE_PATH / "current"
        if current.is_symlink() and current.resolve(strict=True).is_dir():
            return ModelSyncRefresh("retained", "new-process-required")
        raise
    root = workspace_root.resolve(strict=True)
    if not settings.default_alias.startswith("opencode-go/"):
        raise WorkspaceCatalogSyncError("K-Dense default model alias is invalid")
    default_models = tuple(model for model in models if model.alias == settings.default_alias)
    if len(default_models) != 1:
        raise WorkspaceCatalogSyncError("K-Dense default model is unavailable")
    models = (
        *default_models,
        *(model for model in models if model.alias != settings.default_alias),
    )
    with _refresh_lock(root):
        kdense_result = refresh_kdense_catalog(
            workspace_root=root,
            generation=_next_generation(root),
            catalog=ModelCatalog(
                base_url=settings.base_url,
                credential_environment="KDENSE_LITELLM_API_KEY",
                credential_value_sha256=sha256(settings.api_key.encode("utf-8")).hexdigest(),
                models=models,
            ),
            process_reload=process_reload,
        )
    return ModelSyncRefresh(kdense_result.file_freshness, kdense_result.process_freshness)


def refresh_catalog(
    *,
    workspace_root: Path,
    adapter: str,
    catalog: ModelCatalog,
    hermes_process_reload: HermesProcessReload | None = None,
    kdense_process_reload: KdenseProcessReload | None = None,
) -> ModelSyncRefresh:
    root = workspace_root.resolve(strict=True)
    with _refresh_lock(root):
        return _apply_locked(
            root,
            adapter,
            catalog,
            "updated",
            hermes_process_reload,
            kdense_process_reload,
        )


def _apply_locked(
    root: Path,
    adapter: str,
    catalog: ModelCatalog,
    freshness: FileFreshness,
    hermes_process_reload: HermesProcessReload | None,
    kdense_process_reload: KdenseProcessReload | None,
) -> ModelSyncRefresh:
    if adapter == "hermes":
        match freshness:
            case "updated" | "fallback":
                hermes_freshness = freshness
            case "current" | "retained":
                raise WorkspaceCatalogSyncError("Hermes catalog freshness is invalid")
            case unreachable:
                assert_never(unreachable)
        result = refresh_hermes_catalog(
            workspace_root=root,
            generation=_next_generation(root),
            catalog=catalog,
            freshness=hermes_freshness,
            process_reload=hermes_process_reload,
        )
        return ModelSyncRefresh(result.file_freshness, result.process_freshness)
    if adapter == "kdense":
        match freshness:
            case "updated":
                pass
            case "current" | "fallback" | "retained":
                raise WorkspaceCatalogSyncError("K-Dense catalog freshness is invalid")
            case unreachable:
                assert_never(unreachable)
        kdense_result = refresh_kdense_catalog(
            workspace_root=root,
            generation=_next_generation(root),
            catalog=catalog,
            process_reload=kdense_process_reload,
        )
        return ModelSyncRefresh(kdense_result.file_freshness, kdense_result.process_freshness)
    plan = adapter_plan(adapter, root, catalog)
    if all(_target_matches(target.path, target.content) for target in plan.targets):
        return ModelSyncRefresh("current", "new-process-required")
    transaction = WorkspaceCatalogTransaction(
        workspace_root=root,
        generation=_next_generation(root),
    )
    prepared = transaction.prepare(targets=plan.targets, explicit_operator_update=True)
    transaction.commit(cas_token=prepared.cas_token)
    return ModelSyncRefresh(freshness, "new-process-required")


def _refresh_lock(root: Path) -> RecordLock:
    state_root = root / _STATE_RELATIVE_PATH
    ensure_private_directory(state_root, trusted_root=root)
    return RecordLock(state_root / "refresh", root)


def _next_generation(root: Path) -> int:
    transactions = root / _STATE_RELATIVE_PATH / "transactions"
    if not transactions.exists():
        return 1
    existing = [
        int(path.name) for path in transactions.iterdir() if path.is_dir() and path.name.isdecimal()
    ]
    return max(existing, default=0) + 1


def _target_matches(path: Path, expected: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _catalog(settings: ModelSyncSettings, aliases: tuple[str, ...]) -> ModelCatalog:
    candidates = (settings.default_alias, *aliases, *settings.fallback_aliases)
    if any(not is_model_alias(alias) for alias in candidates):
        raise WorkspaceCatalogSyncError("workspace model alias is invalid")
    model_aliases = unique_aliases(candidates)
    if not model_aliases:
        raise WorkspaceCatalogSyncError("workspace model aliases are empty")
    return ModelCatalog(
        base_url=settings.base_url,
        credential_environment="OPENAI_API_KEY",
        credential_value_sha256=sha256(settings.api_key.encode("utf-8")).hexdigest(),
        models=tuple(CatalogModel(alias=alias, display_name=alias) for alias in model_aliases),
    )


def main() -> int:
    command = parse_runtime_command()
    result = refresh_from_environment(
        adapter=command.adapter,
        workspace_root=command.workspace_root,
        hermes_process_reload=command.hermes_process_reload,
        kdense_process_reload=command.kdense_process_reload,
    )
    print(
        json.dumps(
            {
                "file_freshness": result.file_freshness,
                "process_freshness": result.process_freshness,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
