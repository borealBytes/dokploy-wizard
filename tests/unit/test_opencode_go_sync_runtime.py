from __future__ import annotations

from pathlib import Path

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
