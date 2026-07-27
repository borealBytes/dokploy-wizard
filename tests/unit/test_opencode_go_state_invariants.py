from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes
from dokploy_wizard.litellm.catalog_state import CatalogStateError, parse_catalog_state, state_bytes
from tests.unit._opencode_go_state_support import complete_state

_NOW = "2026-07-01T00:00:00Z"
_LATER = "2026-07-02T00:00:00Z"
_EARLIER = "2026-06-30T00:00:00Z"


def _mutated(path: tuple[str | int, ...], replacement: JsonValue) -> bytes:
    decoded: JsonValue = json.loads(state_bytes(complete_state()))
    current = decoded
    for key in path[:-1]:
        if isinstance(key, str):
            assert isinstance(current, dict)
            current = current[key]
        else:
            assert isinstance(current, list)
            current = current[key]
    final = path[-1]
    if isinstance(final, str):
        assert isinstance(current, dict)
        current[final] = replacement
    else:
        assert isinstance(current, list)
        current[final] = replacement
    return canonical_json_bytes(decoded)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("lkg", "status"), "empty"),
        (("lkg", "artifact_sha256"), "c" * 64),
        (("anomaly", "candidate_count"), 1),
        (("anomaly", "state"), "pending"),
        (("anomaly", "state"), "quarantined"),
        (("anomaly", "state"), "confirmed"),
        (("anomaly", "state"), "cleared"),
        (("missing", 0, "second_missing_at"), _LATER),
        (("missing", 0, "state"), "present"),
        (("missing", 0, "state"), "confirmed_absence"),
        (("missing", 0, "state"), "eligible_for_delete"),
        (("missing", 0, "state"), "deleted"),
        (("missing", 0, "state"), "reappeared"),
        (("quarantine", 0, "first_seen_at"), _LATER),
        (("last_result", "status"), "success"),
        (("last_result", "status"), "no_change"),
        (("last_result", "status"), "quarantined"),
        (("last_result", "status"), "failed"),
        (("last_result", "ended_at"), _EARLIER),
    ],
)
def test_state_parser_rejects_impossible_tagged_record_combinations(
    path: tuple[str | int, ...],
    replacement: JsonValue,
) -> None:
    with pytest.raises(CatalogStateError):
        parse_catalog_state(_mutated(path, replacement))


def test_state_parser_rejects_wrong_missing_deletion_boundary() -> None:
    raw = _mutated(("missing", 0, "state"), "confirmed_absence")
    decoded: JsonValue = json.loads(raw)
    assert isinstance(decoded, dict)
    missing = decoded["missing"]
    assert isinstance(missing, list)
    record = missing[0]
    assert isinstance(record, dict)
    record["second_missing_at"] = _LATER
    record["delete_not_before"] = "2026-07-03T00:00:00Z"

    with pytest.raises(CatalogStateError, match="deletion boundary"):
        parse_catalog_state(canonical_json_bytes(decoded))


def test_state_parser_rejects_cross_record_decision_mismatch() -> None:
    with pytest.raises(CatalogStateError, match="decision"):
        parse_catalog_state(_mutated(("lkg", "decision_sha256"), "d" * 64))


def test_state_parser_accepts_semantically_complete_state() -> None:
    state = complete_state()

    assert parse_catalog_state(state_bytes(state)) == state
    assert state.observation is not None
    assert state.observation.provenance[0].observed_at == datetime(2026, 7, 1, tzinfo=UTC)
