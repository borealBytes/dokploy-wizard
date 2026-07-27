from __future__ import annotations

import json

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes
from dokploy_wizard.litellm.catalog_state import CatalogStateError, parse_catalog_state, state_bytes
from tests.unit._opencode_go_state_support import complete_state


def _state_with_provenance_value(
    source_index: int,
    field: str,
    value: str | None,
) -> bytes:
    decoded: JsonValue = json.loads(state_bytes(complete_state()))
    assert isinstance(decoded, dict)
    observation = decoded["observation"]
    assert isinstance(observation, dict)
    provenance = observation["provenance"]
    assert isinstance(provenance, list)
    record = provenance[source_index]
    assert isinstance(record, dict)
    record[field] = value
    return canonical_json_bytes(decoded)


@pytest.mark.parametrize(
    ("source_index", "field", "value"),
    [
        (0, "commit", "c" * 40),
        (0, "blob", "d" * 40),
        (1, "commit", "c" * 40),
        (1, "blob", "d" * 40),
        (2, "commit", None),
        (2, "blob", None),
        (2, "commit", "c" * 40),
        (2, "blob", "d" * 40),
    ],
)
def test_state_parser_rejects_source_specific_provenance_drift(
    source_index: int,
    field: str,
    value: str | None,
) -> None:
    raw = _state_with_provenance_value(source_index, field, value)

    with pytest.raises(CatalogStateError, match="provenance"):
        parse_catalog_state(raw)
