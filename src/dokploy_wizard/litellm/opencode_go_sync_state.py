from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_model_decision import catalog_model_payload
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel, CatalogObservation
from dokploy_wizard.litellm.catalog_persistence import STATE_FILENAME, CatalogGeneration
from dokploy_wizard.litellm.catalog_retirement import (
    CatalogCandidate,
    CatalogTimeline,
    advance_timeline,
)
from dokploy_wizard.litellm.catalog_state import empty_catalog_state, parse_catalog_state
from dokploy_wizard.litellm.catalog_state_types import (
    CatalogState,
    LastResult,
    LkgRecord,
    StateObservation,
    StateQuarantineRecord,
)


@dataclass(frozen=True, slots=True)
class PreparedCatalogSync:
    models: tuple[CatalogModel, ...]
    state: CatalogState
    generation: CatalogGeneration | None


def prepare_catalog_sync(
    state_root: Path,
    catalog_id: str,
    observation: CatalogObservation,
) -> PreparedCatalogSync:
    previous = _load_state(state_root, catalog_id)
    models = observation.visible_models
    generation_payload = canonical_json_bytes(
        {"models": [catalog_model_payload(model) for model in models]}
    )
    output_sha256 = sha256_bytes(generation_payload)
    if previous.last_output_sha256 == output_sha256:
        return PreparedCatalogSync(models, previous, None)
    timeline = advance_timeline(
        _timeline(previous),
        CatalogCandidate(observation.source_ids, observation.accepted_ids),
        _ObservationClock(observation.observed_at),
    )
    if timeline.status == "quarantined_anomalous":
        raise RuntimeError("OpenCode Go catalog shrink is quarantined")
    input_sha256 = sha256_bytes(
        canonical_json_bytes([item.raw_sha256 for item in observation.provenance])
    )
    generation_number = (previous.lkg.generation or 0) + 1
    state = CatalogState(
        1,
        2,
        catalog_id,
        "enabled",
        observation.observed_at,
        observation.observed_at,
        input_sha256,
        output_sha256,
        previous.durable_write_count,
        StateObservation(
            timeline.status,
            observation.observed_at,
            observation.complete,
            observation.source_ids,
            observation.source_ids_sha256,
            len(observation.source_ids),
            observation.accepted_ids,
            observation.accepted_ids_sha256,
            len(observation.accepted_ids),
            observation.decision_sha256,
            observation.provenance,
        ),
        LkgRecord(
            "available",
            generation_number,
            observation.observed_at,
            observation.accepted_ids_sha256,
            output_sha256,
            observation.decision_sha256,
            f"{state_root}/generations/{generation_number}-{output_sha256}.json",
            output_sha256,
        ),
        timeline.timeline.anomaly,
        timeline.timeline.missing,
        tuple(
            StateQuarantineRecord(
                item.source_id,
                item.reason,
                observation.observed_at,
                observation.observed_at,
                item.source_sha256,
                item.preserved_visible_row_sha256,
            )
            for item in observation.quarantine
        ),
        LastResult(
            "success_with_quarantine" if observation.quarantine else "success",
            timeline.status,
            observation.observed_at,
            observation.observed_at,
            input_sha256,
            output_sha256,
            0,
        ),
    )
    return PreparedCatalogSync(
        models,
        state,
        CatalogGeneration(generation_number, output_sha256, generation_payload),
    )


@dataclass(frozen=True, slots=True)
class _ObservationClock:
    observed_at: datetime

    def now(self) -> datetime:
        return self.observed_at


def _load_state(state_root: Path, catalog_id: str) -> CatalogState:
    state_path = state_root / STATE_FILENAME
    if not state_path.exists():
        return empty_catalog_state(catalog_id)
    return parse_catalog_state(state_path.read_bytes())


def _timeline(state: CatalogState) -> CatalogTimeline:
    observation = state.observation
    if observation is None:
        return CatalogTimeline.empty()
    return CatalogTimeline(
        observation.source_ids,
        observation.accepted_ids,
        observation.observed_at,
        state.anomaly,
        state.missing,
    )
