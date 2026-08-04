from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal, Mapping

from dokploy_wizard.litellm.catalog_clock import CatalogClock
from dokploy_wizard.litellm.catalog_observation import CatalogSources, build_observation
from dokploy_wizard.litellm.catalog_persistence import (
    CatalogPersistenceError,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_pricing import PricingContractError
from dokploy_wizard.litellm.catalog_source_parsers import (
    parse_models_dev,
    parse_official,
    parse_zen,
)
from dokploy_wizard.litellm.catalog_sources import (
    MODELS_DEV_SOURCE,
    OFFICIAL_SOURCE,
    ZEN_SOURCE,
    UrllibSourceTransport,
    fetch_source,
)
from dokploy_wizard.litellm.catalog_state import CatalogStateError
from dokploy_wizard.litellm.catalog_types import SourceContractError
from dokploy_wizard.litellm.model_admin_client import LiteLLMModelAdminClient
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminApi,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
)
from dokploy_wizard.litellm.opencode_go_plan import OpenCodeGoReconciliationInput
from dokploy_wizard.litellm.opencode_go_reconciler import OpenCodeGoDatabaseReconciler
from dokploy_wizard.litellm.opencode_go_sync_state import prepare_catalog_sync

SyncFailureCategory = Literal[
    "catalog_source",
    "catalog_state",
    "model_admin_conflict",
    "model_admin_inventory",
    "model_admin_transport",
    "model_admin_unknown",
    "model_admin_unowned_alias",
    "model_admin_write",
    "persistence",
    "runtime_config",
    "runtime_lock",
    "runtime_unknown",
]


@dataclass(frozen=True, slots=True)
class SyncRuntimeError(RuntimeError):
    category: SyncFailureCategory


@dataclass(frozen=True, slots=True)
class SyncRuntimeConfig:
    catalog_id: str
    config_sha256: str
    owner_id: str

    @classmethod
    def load(cls, path: Path, environment: Mapping[str, str]) -> SyncRuntimeConfig:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "catalog_id",
            "config_sha256",
            "owner_id",
            "schema_version",
            "sync_contract_version",
        }:
            raise ValueError("OpenCode Go sync config has an invalid shape")
        owner_id = environment.get("DOKPLOY_WIZARD_SCHEDULE_OWNER_ID")
        if (
            payload["schema_version"] != 1
            or payload["sync_contract_version"] != 2
            or payload["catalog_id"] != "opencode-go"
            or not isinstance(payload["config_sha256"], str)
            or len(payload["config_sha256"]) != 64
            or not isinstance(payload["owner_id"], str)
            or payload["owner_id"] != owner_id
        ):
            raise ValueError("OpenCode Go sync config does not match the runtime")
        return cls(payload["catalog_id"], payload["config_sha256"], payload["owner_id"])


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class SyncRuntimeDependencies:
    clock: CatalogClock
    catalog_loader: Callable[[CatalogClock], CatalogSources]
    model_admin_api: LiteLLMModelAdminApi

    @classmethod
    def live(cls, master_key: str) -> SyncRuntimeDependencies:
        return cls(
            SystemClock(),
            _load_live_sources,
            LiteLLMModelAdminClient(
                api_url="http://127.0.0.1:4000",
                master_key=master_key,
            ),
        )


def synchronize(
    *,
    state_root: Path,
    config: SyncRuntimeConfig,
    dependencies: SyncRuntimeDependencies,
) -> None:
    clock = dependencies.clock
    try:
        sources = dependencies.catalog_loader(clock)
        observation = build_observation(sources, clock)
    except (PricingContractError, SourceContractError) as error:
        raise SyncRuntimeError("catalog_source") from error
    try:
        prepared = prepare_catalog_sync(state_root, config.catalog_id, observation)
    except (CatalogPersistenceError, CatalogStateError, RuntimeError) as error:
        raise SyncRuntimeError("catalog_state") from error
    try:
        OpenCodeGoDatabaseReconciler(dependencies.model_admin_api).reconcile(
            OpenCodeGoReconciliationInput(prepared.models, prepared.state, False)
        )
    except LiteLLMModelAdminError as error:
        raise SyncRuntimeError(_model_admin_failure(error)) from error
    try:
        persist_catalog_transition(state_root, prepared.state, prepared.generation)
    except (CatalogPersistenceError, OSError) as error:
        raise SyncRuntimeError("persistence") from error


def main() -> int:
    try:
        return _run()
    except SyncRuntimeError as error:
        print(f"DOKPLOY_WIZARD_SYNC_ERROR={error.category}", file=sys.stderr)
        return 1
    except Exception:
        print("DOKPLOY_WIZARD_SYNC_ERROR=runtime_unknown", file=sys.stderr)
        return 1


def _run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--max-runtime-seconds", type=int, required=True)
    parser.add_argument("--http-timeout-seconds", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.max_runtime_seconds != 300 or arguments.http_timeout_seconds != 30:
        raise SyncRuntimeError("runtime_config")
    try:
        config = SyncRuntimeConfig.load(arguments.config, os.environ)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SyncRuntimeError("runtime_config") from error
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if master_key == "":
        raise SyncRuntimeError("runtime_config")
    try:
        arguments.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with arguments.lock_file.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not _active_external_lease(arguments.state_dir):
                    raise SyncRuntimeError("runtime_lock") from None
            synchronize(
                state_root=arguments.state_dir,
                config=config,
                dependencies=SyncRuntimeDependencies.live(master_key),
            )
    except OSError as error:
        raise SyncRuntimeError("runtime_lock") from error
    return 0


def _load_live_sources(clock: CatalogClock) -> CatalogSources:
    transport = UrllibSourceTransport()
    return CatalogSources(
        parse_zen(fetch_source(ZEN_SOURCE, transport), clock),
        parse_models_dev(fetch_source(MODELS_DEV_SOURCE, transport), clock),
        parse_official(fetch_source(OFFICIAL_SOURCE, transport), clock),
    )


def _model_admin_failure(error: LiteLLMModelAdminError) -> SyncFailureCategory:
    if error.reason.startswith("unowned alias "):
        return "model_admin_unowned_alias"
    inventory_markers = (
        "inventory",
        "masked routing parameter",
        "routing parameters must contain",
        "deployment blocked must be nested",
    )
    if any(marker in error.reason for marker in inventory_markers):
        return "model_admin_inventory"
    if "transport failed" in error.reason or "request failed with status" in error.reason:
        return "model_admin_transport"
    if isinstance(error, LiteLLMModelAdminWriteAmbiguity):
        return "model_admin_write"
    if isinstance(error, LiteLLMModelAdminConflict):
        return "model_admin_conflict"
    return "model_admin_unknown"
def _active_external_lease(state_root: Path) -> bool:
    now = datetime.now(tz=UTC)
    for path in (state_root / "lease-receipts").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("phase") != "parent_running":
            continue
        deadline = payload.get("heartbeat_deadline_at")
        if isinstance(deadline, str) and datetime.fromisoformat(deadline) >= now:
            return True
    return False
