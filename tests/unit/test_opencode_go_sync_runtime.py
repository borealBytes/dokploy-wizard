from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.opencode_go_sync_package import (
    render_opencode_go_sync_package,
)
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminConflict,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    ModelUuid,
)
from dokploy_wizard.litellm.opencode_go_sync_runtime import (
    SyncRuntimeConfig,
    SyncRuntimeDependencies,
    SyncRuntimeError,
    synchronize,
)
from tests.unit._opencode_go_catalog_support import StaticClock, catalog_sources
from tests.unit._opencode_go_reconciler_support import MemoryModelAdminApi


def test_synchronize_persists_first_generation_then_keeps_identical_state(
    tmp_path: Path,
) -> None:
    # Given
    clock = StaticClock()
    sources = catalog_sources(clock)
    api = MemoryModelAdminApi([], [], [], [])
    dependencies = SyncRuntimeDependencies(clock, lambda _: sources, api)
    config = SyncRuntimeConfig("opencode-go", "a" * 64, "owner")

    # When
    synchronize(state_root=tmp_path, config=config, dependencies=dependencies)
    first_state = (tmp_path / "opencode-go-sync-state-v1.json").read_bytes()
    first_mutations = tuple(api.mutations)
    synchronize(state_root=tmp_path, config=config, dependencies=dependencies)

    # Then
    assert first_mutations
    assert tuple(api.mutations) == first_mutations
    assert (tmp_path / "opencode-go-sync-state-v1.json").read_bytes() == first_state


def test_packaged_runtime_reports_fixed_config_category_without_traceback(
    tmp_path: Path,
) -> None:
    # Given
    package = tmp_path / "opencode_go_sync.py"
    package.write_bytes(render_opencode_go_sync_package())

    # When
    result = subprocess.run(
        (
            sys.executable,
            str(package),
            "--config",
            str(tmp_path / "missing.json"),
            "--state-dir",
            str(tmp_path / "state"),
            "--lock-file",
            str(tmp_path / "state" / "sync.lock"),
            "--once",
            "--max-runtime-seconds",
            "300",
            "--http-timeout-seconds",
            "30",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID": "owner"},
    )

    # Then
    assert result.returncode == 1
    assert result.stderr == "DOKPLOY_WIZARD_SYNC_ERROR=runtime_config\n"


class UnownedAliasApi:
    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        raise LiteLLMModelAdminConflict("unowned alias opencode-go/example")

    def create_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError(deployment.model_name)

    def update_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError(deployment.model_name)

    def delete_model(self, model_id: ModelUuid) -> None:
        raise AssertionError(model_id)


class MaskedInventoryApi(UnownedAliasApi):
    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        raise LiteLLMModelAdminConflict(
            "LiteLLM model inventory contains a masked routing parameter"
        )


class MissingDataArrayApi(UnownedAliasApi):
    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        raise LiteLLMModelAdminConflict(
            "LiteLLM model inventory requires a data array"
        )


def test_synchronize_classifies_unowned_model_alias_without_exposing_name(
    tmp_path: Path,
) -> None:
    sources = catalog_sources()
    dependencies = SyncRuntimeDependencies(
        StaticClock(),
        lambda _: sources,
        UnownedAliasApi(),
    )

    with pytest.raises(SyncRuntimeError) as captured:
        synchronize(
            state_root=tmp_path,
            config=SyncRuntimeConfig("opencode-go", "a" * 64, "owner"),
            dependencies=dependencies,
        )

    assert captured.value.category == "model_admin_unowned_alias"


def test_synchronize_classifies_masked_inventory_without_exposing_payload(
    tmp_path: Path,
) -> None:
    sources = catalog_sources()
    dependencies = SyncRuntimeDependencies(
        StaticClock(),
        lambda _: sources,
        MaskedInventoryApi(),
    )

    with pytest.raises(SyncRuntimeError) as captured:
        synchronize(
            state_root=tmp_path,
            config=SyncRuntimeConfig("opencode-go", "a" * 64, "owner"),
            dependencies=dependencies,
        )

    assert captured.value.category == "model_admin_inventory_masked"


def test_synchronize_classifies_missing_inventory_data_array(
    tmp_path: Path,
) -> None:
    sources = catalog_sources()
    dependencies = SyncRuntimeDependencies(
        StaticClock(),
        lambda _: sources,
        MissingDataArrayApi(),
    )

    with pytest.raises(SyncRuntimeError) as captured:
        synchronize(
            state_root=tmp_path,
            config=SyncRuntimeConfig("opencode-go", "a" * 64, "owner"),
            dependencies=dependencies,
        )

    assert captured.value.category == "model_admin_inventory_data"
