from __future__ import annotations

import os
from pathlib import Path

from dokploy_wizard.litellm.catalog_persistence import (
    STATE_FILENAME,
    CatalogPersistenceTransition,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_state import empty_catalog_state, state_bytes
from tests.unit.test_opencode_go_persistence import _generation, _state_for


def test_persistence_replaces_exact_observed_state_with_next_generation(
    tmp_path: Path,
) -> None:
    # Given
    previous = empty_catalog_state("opencode-go")
    previous_bytes = state_bytes(previous)
    state_path = tmp_path / STATE_FILENAME
    state_path.write_bytes(previous_bytes)
    os.chmod(state_path, 0o600)
    generation = _generation()

    # When
    committed = persist_catalog_transition(
        tmp_path,
        CatalogPersistenceTransition(previous_bytes, _state_for(generation), generation),
    )

    # Then
    assert state_path.read_bytes() == state_bytes(committed)
