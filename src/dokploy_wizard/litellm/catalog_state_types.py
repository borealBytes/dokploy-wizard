from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from dokploy_wizard.litellm.catalog_missing import MissingRecord
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_types import (
    ObservationStatus,
    QuarantineReason,
    SourceProvenance,
)

RuntimeState = Literal["enabled", "disabled", "blocked"]
LkgStatus = Literal["empty", "available"]
ResultStatus = Literal[
    "success",
    "success_with_quarantine",
    "no_change",
    "quarantined",
    "failed",
]


@dataclass(frozen=True, slots=True)
class StateObservation:
    status: ObservationStatus
    observed_at: datetime
    complete: bool
    source_ids: tuple[str, ...]
    source_ids_sha256: str
    source_ids_count: int
    accepted_ids: tuple[str, ...]
    accepted_ids_sha256: str
    accepted_ids_count: int
    decision_sha256: str
    provenance: tuple[SourceProvenance, ...]


@dataclass(frozen=True, slots=True)
class LkgRecord:
    status: LkgStatus
    generation: int | None
    accepted_at: datetime | None
    accepted_ids_sha256: str | None
    model_set_sha256: str | None
    decision_sha256: str | None
    artifact_path: str | None
    artifact_sha256: str | None

    @classmethod
    def empty(cls) -> LkgRecord:
        return cls("empty", None, None, None, None, None, None, None)


@dataclass(frozen=True, slots=True)
class StateQuarantineRecord:
    source_id: str
    reason: QuarantineReason
    first_seen_at: datetime
    last_seen_at: datetime
    source_sha256: str
    preserved_visible_row_sha256: str | None


@dataclass(frozen=True, slots=True)
class LastResult:
    status: ResultStatus
    reason: str
    started_at: datetime
    ended_at: datetime
    input_sha256: str | None
    output_sha256: str | None
    durable_write_delta: int


@dataclass(frozen=True, slots=True)
class CatalogState:
    schema_version: Literal[1]
    sync_contract_version: Literal[2]
    catalog_id: str
    state: RuntimeState
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_input_sha256: str | None
    last_output_sha256: str | None
    durable_write_count: int
    observation: StateObservation | None
    lkg: LkgRecord
    anomaly: AnomalyRecord
    missing: tuple[MissingRecord, ...]
    quarantine: tuple[StateQuarantineRecord, ...]
    last_result: LastResult | None


@dataclass(frozen=True, slots=True)
class CatalogStateTransition:
    state: CatalogState
    observation_status: ObservationStatus
    persist: bool
    durable_write_delta: int
