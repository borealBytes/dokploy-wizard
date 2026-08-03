"""Typed environment and CLI inputs for workspace catalog synchronization."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Mapping, assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_contracts import require_internal_base_url
from dokploy_wizard.dokploy.workspace_catalog_sync_hermes import (
    HermesProcessReload,
    load_hermes_process_reload,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_kdense import (
    KdenseProcessReload,
    load_kdense_process_reload,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    WorkspaceCatalogSyncError,
)

AdapterName = Literal["primary", "opencode-web", "hermes", "kdense"]
_ADAPTER_NAMES: Final[tuple[AdapterName, ...]] = (
    "primary",
    "opencode-web",
    "hermes",
    "kdense",
)


@dataclass(frozen=True, slots=True)
class ModelSyncSettings:
    base_url: str
    api_key: str
    default_alias: str
    fallback_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    adapter: AdapterName
    workspace_root: Path
    hermes_process_reload: HermesProcessReload | None
    kdense_process_reload: KdenseProcessReload | None


def settings_from_environment(environ: Mapping[str, str]) -> ModelSyncSettings:
    base_url = require_internal_base_url(_required(environ, "OPENAI_API_BASE"))
    default_alias = _required(environ, "DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS")
    fallback_aliases = _parse_fallback_aliases(
        _required(environ, "DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON")
    )
    return ModelSyncSettings(
        base_url=base_url,
        api_key=_required(environ, "OPENAI_API_KEY"),
        default_alias=default_alias,
        fallback_aliases=fallback_aliases,
    )


def kdense_settings_from_environment(environ: Mapping[str, str]) -> ModelSyncSettings:
    return ModelSyncSettings(
        base_url=require_internal_base_url(_required(environ, "KDENSE_LITELLM_BASE_URL")),
        api_key=_required(environ, "KDENSE_LITELLM_API_KEY"),
        default_alias=_required(environ, "DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS"),
        fallback_aliases=(),
    )


def parse_runtime_command() -> RuntimeCommand:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", choices=_ADAPTER_NAMES, required=True)
    parser.add_argument("--workspace-root", type=Path, default=Path.home())
    parser.add_argument("--hermes-pid-file", type=Path)
    parser.add_argument("--hermes-supervisor", type=Path)
    parser.add_argument("--kdense-pid-file", type=Path)
    parser.add_argument("--kdense-supervisor", type=Path)
    arguments = parser.parse_args()
    if (arguments.hermes_pid_file is None) != (arguments.hermes_supervisor is None):
        parser.error("Hermes reload needs both --hermes-pid-file and --hermes-supervisor")
    hermes_process_reload = None
    if arguments.hermes_pid_file is not None and arguments.hermes_supervisor is not None:
        hermes_process_reload = load_hermes_process_reload(
            workspace_root=arguments.workspace_root,
            pid_file=arguments.hermes_pid_file,
            supervisor=arguments.hermes_supervisor,
        )
    if (arguments.kdense_pid_file is None) != (arguments.kdense_supervisor is None):
        parser.error("K-Dense reload needs both --kdense-pid-file and --kdense-supervisor")
    kdense_process_reload = None
    if arguments.kdense_pid_file is not None and arguments.kdense_supervisor is not None:
        kdense_process_reload = load_kdense_process_reload(
            workspace_root=arguments.workspace_root,
            pid_file=arguments.kdense_pid_file,
            supervisor=arguments.kdense_supervisor,
        )
    return RuntimeCommand(
        adapter=arguments.adapter,
        workspace_root=arguments.workspace_root,
        hermes_process_reload=hermes_process_reload,
        kdense_process_reload=kdense_process_reload,
    )


def is_model_alias(value: str) -> bool:
    return (
        value == value.strip()
        and "/" in value
        and not value.endswith("/*")
        and not value.startswith("openai/")
    )


def unique_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _parse_fallback_aliases(value: str) -> tuple[str, ...]:
    try:
        payload: JsonValue = json.loads(value)
    except json.JSONDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace fallback aliases are invalid") from error
    match payload:
        case list() as aliases:
            result = tuple(_fallback_alias(alias) for alias in aliases)
        case None | str() | int() | float() | dict():
            raise WorkspaceCatalogSyncError("workspace fallback aliases are invalid")
        case unreachable:
            assert_never(unreachable)
    return unique_aliases(result)


def _fallback_alias(value: JsonValue) -> str:
    match value:
        case str() as alias if is_model_alias(alias):
            return alias
        case None | str() | int() | float() | list() | dict():
            raise WorkspaceCatalogSyncError("workspace fallback model alias is invalid")
        case unreachable:
            assert_never(unreachable)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or value == "":
        raise WorkspaceCatalogSyncError(f"workspace {name} is unavailable")
    return value
