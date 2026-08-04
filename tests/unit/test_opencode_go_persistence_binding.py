from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.litellm.catalog_persistence import (
    CatalogGeneration,
    CatalogPersistenceError,
    CatalogPersistenceTransition,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_state_types import CatalogState, LkgRecord
from tests.unit._opencode_go_state_support import complete_state

GenerationBindingMismatch = Literal[
    "payload",
    "lkg_generation",
    "lkg_model_hash",
    "lkg_artifact_hash",
    "lkg_artifact_filename",
    "last_output_hash",
    "result_output_hash",
    "lkg_empty",
    "result_status",
]


def _generation(payload: bytes = b'{"models":["a"]}') -> CatalogGeneration:
    return CatalogGeneration(1, hashlib.sha256(payload).hexdigest(), payload)


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


def _unbound_inputs(
    mismatch: GenerationBindingMismatch,
) -> tuple[CatalogState, CatalogGeneration]:
    generation = _generation()
    state = _state_for(generation)
    assert state.last_result is not None
    match mismatch:
        case "payload":
            return state, _generation(b'{"models":["b"]}')
        case "lkg_generation":
            return replace(state, lkg=replace(state.lkg, generation=2)), generation
        case "lkg_model_hash":
            return (
                replace(
                    state,
                    lkg=replace(state.lkg, model_set_sha256="b" * 64),
                ),
                generation,
            )
        case "lkg_artifact_hash":
            return (
                replace(state, lkg=replace(state.lkg, artifact_sha256="b" * 64)),
                generation,
            )
        case "lkg_artifact_filename":
            return (
                replace(
                    state,
                    lkg=replace(state.lkg, artifact_path="/state/generations/foreign.json"),
                ),
                generation,
            )
        case "last_output_hash":
            return replace(state, last_output_sha256="b" * 64), generation
        case "result_output_hash":
            return (
                replace(
                    state,
                    last_result=replace(state.last_result, output_sha256="b" * 64),
                ),
                generation,
            )
        case "lkg_empty":
            return replace(state, lkg=LkgRecord.empty()), generation
        case "result_status":
            return (
                replace(
                    state,
                    last_result=replace(
                        state.last_result,
                        status="no_change",
                        durable_write_delta=0,
                    ),
                ),
                generation,
            )
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize(
    "mismatch",
    [
        "payload",
        "lkg_generation",
        "lkg_model_hash",
        "lkg_artifact_hash",
        "lkg_artifact_filename",
        "last_output_hash",
        "result_output_hash",
        "lkg_empty",
        "result_status",
    ],
)
def test_generation_publication_rejects_every_unbound_state_before_directory_creation(
    tmp_path: Path,
    mismatch: GenerationBindingMismatch,
) -> None:
    state, generation = _unbound_inputs(mismatch)
    state_root = tmp_path / "state"

    with pytest.raises(CatalogPersistenceError, match="generation does not bind state"):
        persist_catalog_transition(
            state_root,
            CatalogPersistenceTransition(None, state, generation),
        )

    assert not state_root.exists()
