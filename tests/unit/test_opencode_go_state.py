from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.litellm.catalog_persistence import (
    CatalogGeneration,
    CatalogPersistenceTransition,
    persist_catalog_transition,
)
from dokploy_wizard.litellm.catalog_state import (
    CatalogStateError,
    empty_catalog_state,
    parse_catalog_state,
    rejected_transition,
    state_bytes,
)
from dokploy_wizard.litellm.catalog_types import ObservationStatus
from tests.unit._opencode_go_state_support import complete_state


def test_state_is_strict_canonical_and_contains_explicit_nulls() -> None:
    state = empty_catalog_state("opencode-go")

    encoded = state_bytes(state)
    decoded = json.loads(encoded)

    assert encoded == json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    assert decoded["schema_version"] == 1
    assert decoded["sync_contract_version"] == 2
    assert decoded["last_attempt_at"] is None
    assert parse_catalog_state(encoded) == state


def test_state_rejects_unknown_keys_and_versions() -> None:
    decoded = json.loads(state_bytes(empty_catalog_state("opencode-go")))
    decoded["unknown"] = True
    with pytest.raises(CatalogStateError, match="keys"):
        parse_catalog_state(json.dumps(decoded).encode())
    decoded.pop("unknown")
    decoded["schema_version"] = 2
    with pytest.raises(CatalogStateError, match="version"):
        parse_catalog_state(json.dumps(decoded).encode())


@pytest.mark.parametrize(
    "status",
    ["rejected_invalid", "rejected_clock_regression", "quarantined_anomalous"],
)
def test_rejected_transition_preserves_previous_bytes_exactly(
    status: ObservationStatus,
) -> None:
    previous = empty_catalog_state("opencode-go")
    before = state_bytes(previous)

    transition = rejected_transition(previous, status)

    assert transition.persist is False
    assert transition.durable_write_delta == 0
    assert state_bytes(transition.state) == before


def test_generation_then_state_persistence_counts_committed_files(tmp_path: Path) -> None:
    payload = b'{"models":["a"]}'
    model_hash = hashlib.sha256(payload).hexdigest()
    generation = CatalogGeneration(1, model_hash, payload)
    state = complete_state()
    assert state.last_result is not None
    state = replace(
        state,
        last_output_sha256=model_hash,
        lkg=replace(
            state.lkg,
            model_set_sha256=model_hash,
            artifact_path=f"/state/generations/1-{model_hash}.json",
            artifact_sha256=model_hash,
        ),
        last_result=replace(state.last_result, output_sha256=model_hash),
    )

    committed = persist_catalog_transition(
        tmp_path,
        CatalogPersistenceTransition(None, state, generation),
    )

    generation_path = tmp_path / "generations" / f"1-{model_hash}.json"
    state_path = tmp_path / "opencode-go-sync-state-v1.json"
    assert generation_path.read_bytes() == payload
    assert parse_catalog_state(state_path.read_bytes()) == committed
    assert committed.durable_write_count == 4
    assert os.stat(generation_path).st_mode & 0o777 == 0o600
    assert os.stat(state_path).st_mode & 0o777 == 0o600


def test_no_change_transition_keeps_state_file_byte_identical(tmp_path: Path) -> None:
    state = empty_catalog_state("opencode-go")
    state_path = tmp_path / "opencode-go-sync-state-v1.json"
    state_path.write_bytes(state_bytes(state))
    os.chmod(state_path, 0o600)
    before = state_path.read_bytes()

    committed = persist_catalog_transition(
        tmp_path,
        CatalogPersistenceTransition(before, state, None),
    )

    assert state_path.read_bytes() == before
    assert committed == state


def test_complete_state_round_trip_preserves_provenance_without_headers() -> None:
    state = complete_state()

    encoded = state_bytes(state)

    assert parse_catalog_state(encoded) == state
    assert b"authorization" not in encoded.lower()
    assert b"cookie" not in encoded.lower()
