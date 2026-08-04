from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import assert_never

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_persistence_fs import (
    CatalogPersistenceError as CatalogPersistenceError,
)
from dokploy_wizard.litellm.catalog_persistence_fs import (
    atomic_write,
    publish_immutable,
    read_contract_file,
    secure_directory,
    validate_existing_contract,
)
from dokploy_wizard.litellm.catalog_state import state_bytes
from dokploy_wizard.litellm.catalog_state_invariants import validate_catalog_state
from dokploy_wizard.litellm.catalog_state_types import CatalogState
from dokploy_wizard.litellm.catalog_state_validation import StateRecordError

STATE_FILENAME = "opencode-go-sync-state-v1.json"


@dataclass(frozen=True, slots=True)
class CatalogGeneration:
    generation: int
    model_set_sha256: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class CatalogPersistenceTransition:
    expected_state_bytes: bytes | None
    state: CatalogState
    generation: CatalogGeneration | None


def persist_catalog_transition(
    state_root: Path,
    transition: CatalogPersistenceTransition,
) -> CatalogState:
    state = transition.state
    generation = transition.generation
    if generation is None:
        existing_state = validate_existing_contract(state_root, STATE_FILENAME)
        if existing_state not in {
            transition.expected_state_bytes,
            state_bytes(state),
        }:
            raise CatalogPersistenceError("existing state bytes are unknown")
        return state
    if generation.generation < 1:
        raise CatalogPersistenceError("generation number must be positive")
    if sha256_bytes(generation.payload) != generation.model_set_sha256:
        raise CatalogPersistenceError("generation hash does not match payload")
    _require_canonical_generation(generation.payload)
    _require_generation_state_binding(state, generation)
    committed_result = (
        None
        if state.last_result is None
        else replace(state.last_result, durable_write_delta=2)
    )
    committed = replace(
        state,
        durable_write_count=state.durable_write_count + 2,
        last_result=committed_result,
    )
    secure_directory(state_root)
    generations = state_root / "generations"
    secure_directory(generations)
    generation_path = generations / (
        f"{generation.generation}-{generation.model_set_sha256}.json"
    )
    state_path = state_root / STATE_FILENAME
    existing_generation = read_contract_file(generation_path, "generation")
    existing_state = read_contract_file(state_path, "state")
    committed_state_bytes = state_bytes(committed)
    if existing_generation is not None and existing_generation != generation.payload:
        raise CatalogPersistenceError("existing generation bytes are unknown")
    if existing_state not in {
        transition.expected_state_bytes,
        committed_state_bytes,
    }:
        raise CatalogPersistenceError("existing state bytes are unknown")
    if existing_state == committed_state_bytes and existing_generation is None:
        raise CatalogPersistenceError("committed state generation is missing")
    if existing_generation is None:
        publish_immutable(generation_path, generation.payload)
    if existing_state != committed_state_bytes:
        atomic_write(state_path, committed_state_bytes)
    return committed


def _require_canonical_generation(payload: bytes) -> None:
    try:
        value: JsonValue = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogPersistenceError("generation payload is invalid JSON") from error
    if canonical_json_bytes(value) != payload:
        raise CatalogPersistenceError("generation payload is not canonical JSON")


def _require_generation_state_binding(
    state: CatalogState,
    generation: CatalogGeneration,
) -> None:
    lkg = state.lkg
    result = state.last_result
    expected_filename = f"{generation.generation}-{generation.model_set_sha256}.json"
    if (
        lkg.status != "available"
        or lkg.generation != generation.generation
        or lkg.model_set_sha256 != generation.model_set_sha256
        or lkg.artifact_sha256 != generation.model_set_sha256
        or lkg.artifact_path is None
        or PurePosixPath(lkg.artifact_path).name != expected_filename
        or state.last_output_sha256 != generation.model_set_sha256
        or result is None
        or result.output_sha256 != generation.model_set_sha256
    ):
        raise CatalogPersistenceError("generation does not bind state")
    match result.status:
        case "success" | "success_with_quarantine":
            pass
        case "no_change" | "quarantined" | "failed":
            raise CatalogPersistenceError("generation does not bind state")
        case unreachable:
            assert_never(unreachable)
    try:
        validate_catalog_state(state)
    except StateRecordError as error:
        raise CatalogPersistenceError("generation does not bind state") from error
