from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_observation import CatalogSources
from dokploy_wizard.litellm.catalog_sources import (
    MODELS_DEV_SOURCE,
    OFFICIAL_SOURCE,
    ZEN_SOURCE,
    FetchDocument,
    parse_models_dev,
    parse_official,
    parse_zen,
)


class MixedFixture(TypedDict):
    official_endpoints: list[list[str]]
    official_pricing: list[list[str]]
    quarantined_ids: list[str]
    valid_ids: list[str]
    zen_ids: list[str]


@dataclass(frozen=True, slots=True)
class StaticClock:
    value: datetime = datetime(2026, 7, 1, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def mixed_fixture() -> MixedFixture:
    path = Path(__file__).parents[1] / "fixtures" / "opencode-go-mixed-catalog-v1.json"
    fixture: MixedFixture = json.loads(path.read_text())
    return fixture


def catalog_sources(clock: StaticClock | None = None) -> CatalogSources:
    observed_clock = StaticClock() if clock is None else clock
    fixture = mixed_fixture()
    zen_payload = {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 1, "owned_by": "opencode"}
            for model_id in fixture["zen_ids"]
        ],
    }
    models: dict[str, JsonValue] = {}
    pricing = {
        "".join(
            character
            for character in row[0].split(" (")[0].lower()
            if character.isalnum()
        ): row
        for row in fixture["official_pricing"]
    }
    endpoint_names = {row[1]: row[0] for row in fixture["official_endpoints"]}
    for model_id in fixture["zen_ids"]:
        if model_id == "hy3-preview":
            continue
        name = endpoint_names.get(model_id, model_id)
        row = pricing.get("".join(character for character in name.lower() if character.isalnum()))
        cost: JsonValue = (
            None
            if row is None
            else {
                "input": float(row[1].removeprefix("$")),
                "output": float(row[2].removeprefix("$")),
                "cache_read": None if row[3] == "-" else float(row[3].removeprefix("$")),
                "cache_write": None if row[4] == "-" else float(row[4].removeprefix("$")),
            }
        )
        models[model_id] = {
            "id": model_id,
            "name": name,
            "cost": cost,
            "limit": {"context": 200000, "output": 32000},
            "modalities": {"input": ["text"], "output": ["text"]},
        }
    models_payload: JsonValue = {
        "opencode-go": {"id": "opencode-go", "models": models, "npm": "informational"}
    }
    official_raw = (
        "\n".join(
            [
                "| Model | Input | Output | Cached Read | Cached Write | Usage |",
                "| --- | --- | --- | --- | --- | --- |",
                *("| " + " | ".join(row) + " |" for row in fixture["official_pricing"]),
                "| Model | Model ID | Endpoint | AI SDK Package |",
                "| --- | --- | --- | --- |",
                *("| " + " | ".join(row) + " |" for row in fixture["official_endpoints"]),
            ]
        )
        + "\n"
    ).encode()
    official_source = OFFICIAL_SOURCE.with_expected_sha256(
        hashlib.sha256(official_raw).hexdigest()
    )
    return CatalogSources(
        parse_zen(
            FetchDocument(ZEN_SOURCE, json.dumps(zen_payload).encode(), "application/json"),
            observed_clock,
        ),
        parse_models_dev(
            FetchDocument(
                MODELS_DEV_SOURCE,
                json.dumps(models_payload).encode(),
                "application/json",
            ),
            observed_clock,
        ),
        parse_official(
            FetchDocument(official_source, official_raw, "text/plain"),
            observed_clock,
        ),
    )
