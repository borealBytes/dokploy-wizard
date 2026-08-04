from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dokploy_wizard.dokploy.opencode_go_sync_package import (
    render_opencode_go_sync_package,
)
from dokploy_wizard.litellm.opencode_go_sync_runtime import (
    SyncRuntimeConfig,
    SyncRuntimeDependencies,
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
