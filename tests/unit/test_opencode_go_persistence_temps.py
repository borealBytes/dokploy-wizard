from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.litellm.catalog_persistence import (
    STATE_FILENAME,
    CatalogGeneration,
    CatalogPersistenceError,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_state_types import CatalogState
from tests.unit._opencode_go_state_support import complete_state


def _generation() -> CatalogGeneration:
    payload = b'{"models":["a"]}'
    return CatalogGeneration(1, hashlib.sha256(payload).hexdigest(), payload)


def _state_for(generation: CatalogGeneration) -> CatalogState:
    state = complete_state()
    assert state.last_result is not None
    return replace(
        state,
        last_output_sha256=generation.model_set_sha256,
        lkg=replace(
            state.lkg,
            model_set_sha256=generation.model_set_sha256,
            artifact_path=f"/state/generations/1-{generation.model_set_sha256}.json",
            artifact_sha256=generation.model_set_sha256,
        ),
        last_result=replace(state.last_result, output_sha256=generation.model_set_sha256),
    )


def test_persistence_does_not_remove_foreign_generation_temp_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _generation()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    monkeypatch.setattr(
        "dokploy_wizard.litellm.catalog_persistence_fs.secrets.token_hex",
        lambda _size: "fixed",
    )
    generation_name = f"1-{generation.model_set_sha256}.json"
    temporary = generations / f".{generation_name}.fixed.tmp"
    temporary.symlink_to(tmp_path / "foreign")

    with pytest.raises(CatalogPersistenceError, match="temporary"):
        persist_catalog_transition(root, _state_for(generation), generation)

    assert temporary.is_symlink()


def test_persistence_does_not_remove_foreign_state_temp_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _generation()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    generation_path = generations / f"1-{generation.model_set_sha256}.json"
    generation_path.write_bytes(generation.payload)
    os.chmod(generation_path, 0o600)
    monkeypatch.setattr(
        "dokploy_wizard.litellm.catalog_persistence_fs.secrets.token_hex",
        lambda _size: "fixed",
    )
    temporary = root / f".{STATE_FILENAME}.fixed.tmp"
    temporary.symlink_to(tmp_path / "foreign")

    with pytest.raises(CatalogPersistenceError, match="temporary"):
        persist_catalog_transition(root, _state_for(generation), generation)

    assert temporary.is_symlink()
