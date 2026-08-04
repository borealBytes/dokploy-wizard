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
    CatalogPersistenceTransition,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_state import empty_catalog_state, state_bytes
from dokploy_wizard.litellm.catalog_state_types import CatalogState
from tests.unit._opencode_go_state_support import complete_state


def _generation() -> CatalogGeneration:
    payload = b'{"models":["a"]}'
    return CatalogGeneration(1, hashlib.sha256(payload).hexdigest(), payload)


def _generation_path(root: Path, generation: CatalogGeneration) -> Path:
    return root / "generations" / (
        f"{generation.generation}-{generation.model_set_sha256}.json"
    )


def _state_for(generation: CatalogGeneration) -> CatalogState:
    state = complete_state()
    assert state.last_result is not None
    return replace(
        state,
        last_output_sha256=generation.model_set_sha256,
        lkg=replace(
            state.lkg,
            generation=generation.generation,
            model_set_sha256=generation.model_set_sha256,
            artifact_path=(
                f"/state/generations/{generation.generation}-"
                f"{generation.model_set_sha256}.json"
            ),
            artifact_sha256=generation.model_set_sha256,
        ),
        last_result=replace(state.last_result, output_sha256=generation.model_set_sha256),
    )


def _transition(
    state: CatalogState,
    generation: CatalogGeneration | None,
    expected_state_bytes: bytes | None = None,
) -> CatalogPersistenceTransition:
    return CatalogPersistenceTransition(expected_state_bytes, state, generation)


@pytest.mark.parametrize("unsafe_path", ["root", "generations", "state", "generation"])
def test_persistence_rejects_every_symlinked_contract_path(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    root = tmp_path / "state"
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    generation = _generation()
    if unsafe_path == "root":
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        generations = root / "generations"
        if unsafe_path == "generations":
            generations.symlink_to(target, target_is_directory=True)
        else:
            generations.mkdir(mode=0o700)
            path = (
                root / STATE_FILENAME
                if unsafe_path == "state"
                else _generation_path(root, generation)
            )
            path.symlink_to(tmp_path / "missing")

    with pytest.raises(CatalogPersistenceError, match="symlink"):
        persist_catalog_transition(root, _transition(_state_for(generation), generation))


@pytest.mark.parametrize("unsafe_path", ["root", "generations", "state", "generation"])
def test_persistence_rejects_mode_drift(tmp_path: Path, unsafe_path: str) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    generation = _generation()
    state = _state_for(generation)
    state_path = root / STATE_FILENAME
    generation_path = _generation_path(root, generation)
    generation_path.write_bytes(generation.payload)
    state_path.write_bytes(state_bytes(state))
    os.chmod(generation_path, 0o600)
    os.chmod(state_path, 0o600)
    selected = {
        "root": root,
        "generations": generations,
        "state": state_path,
        "generation": generation_path,
    }[unsafe_path]
    os.chmod(selected, 0o755 if selected.is_dir() else 0o640)

    with pytest.raises(CatalogPersistenceError, match="mode"):
        persist_catalog_transition(
            root,
            _transition(state, generation, state_bytes(state)),
        )


def test_persistence_rejects_unknown_existing_generation_bytes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    generation = _generation()
    generation_path = _generation_path(root, generation)
    generation_path.write_bytes(b'{"models":["foreign"]}')
    os.chmod(generation_path, 0o600)

    with pytest.raises(CatalogPersistenceError, match="generation bytes"):
        persist_catalog_transition(root, _transition(_state_for(generation), generation))

    assert generation_path.read_bytes() == b'{"models":["foreign"]}'


def test_generation_before_state_crash_retries_without_replacing_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    generation = _generation()
    generation_path = _generation_path(root, generation)
    generation_path.write_bytes(generation.payload)
    os.chmod(generation_path, 0o600)
    generation_inode = generation_path.stat().st_ino

    committed = persist_catalog_transition(
        root,
        _transition(_state_for(generation), generation),
    )

    assert generation_path.stat().st_ino == generation_inode
    assert committed.durable_write_count == 4
    assert (root / STATE_FILENAME).read_bytes() == state_bytes(committed)


def test_partial_retry_accepts_only_exact_previous_state_bytes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    generation = _generation()
    generation_path = _generation_path(root, generation)
    generation_path.write_bytes(generation.payload)
    os.chmod(generation_path, 0o600)
    state = _state_for(generation)
    state_path = root / STATE_FILENAME
    state_path.write_bytes(state_bytes(state))
    os.chmod(state_path, 0o600)
    generation_inode = generation_path.stat().st_ino

    committed = persist_catalog_transition(
        root,
        _transition(state, generation, state_bytes(state)),
    )

    assert generation_path.stat().st_ino == generation_inode
    assert state_path.read_bytes() == state_bytes(committed)


def test_completed_retry_is_byte_and_inode_identical(tmp_path: Path) -> None:
    generation = _generation()
    state = _state_for(generation)
    transition = _transition(state, generation)
    committed = persist_catalog_transition(tmp_path, transition)
    generation_path = _generation_path(tmp_path, generation)
    state_path = tmp_path / STATE_FILENAME
    before = (
        generation_path.stat().st_ino,
        state_path.stat().st_ino,
        generation_path.read_bytes(),
        state_path.read_bytes(),
    )

    retried = persist_catalog_transition(tmp_path, transition)

    assert retried == committed
    assert (
        generation_path.stat().st_ino,
        state_path.stat().st_ino,
        generation_path.read_bytes(),
        state_path.read_bytes(),
    ) == before


def test_persistence_rejects_unknown_existing_state_bytes(tmp_path: Path) -> None:
    generation = _generation()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    state_path = root / STATE_FILENAME
    state_path.write_bytes(
        state_bytes(replace(empty_catalog_state("foreign"), durable_write_count=2))
    )
    os.chmod(state_path, 0o600)

    with pytest.raises(CatalogPersistenceError, match="state bytes"):
        persist_catalog_transition(root, _transition(_state_for(generation), generation))


def test_no_change_rejects_unknown_existing_state_bytes(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    state_path.write_bytes(state_bytes(empty_catalog_state("foreign")))
    os.chmod(state_path, 0o600)

    with pytest.raises(CatalogPersistenceError, match="state bytes"):
        persist_catalog_transition(
            tmp_path,
            _transition(empty_catalog_state("opencode-go"), None),
        )


def test_persistence_rejects_committed_state_without_generation(tmp_path: Path) -> None:
    generation = _generation()
    state = _state_for(generation)
    committed = replace(
        state,
        durable_write_count=state.durable_write_count + 2,
        last_result=replace(state.last_result, durable_write_delta=2)
        if state.last_result is not None
        else None,
    )
    state_path = tmp_path / STATE_FILENAME
    state_path.write_bytes(state_bytes(committed))
    os.chmod(state_path, 0o600)

    with pytest.raises(CatalogPersistenceError, match="generation is missing"):
        persist_catalog_transition(tmp_path, _transition(state, generation))
