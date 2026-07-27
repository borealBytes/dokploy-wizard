from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from dokploy_wizard.litellm.catalog_projection import ModelDecisionProjection
from dokploy_wizard.litellm.catalog_types import (
    Modalities,
    ModelLimits,
    ModelsDevSnapshot,
    ObservationStatus,
    OfficialSnapshot,
    QuarantineReason,
    ResolvedPricing,
    SourceProvenance,
    TransportName,
    ZenSnapshot,
)


@dataclass(frozen=True, slots=True)
class CatalogModel:
    source_id: str
    name: str
    transport: TransportName
    endpoint: str
    package: str
    pricing: ResolvedPricing
    limits: ModelLimits
    modalities: Modalities
    pricing_tiers_sha256: str
    pricing_selection_sha256: str
    projection: ModelDecisionProjection
    merged_decision_sha256: str

    def with_source_id(self, source_id: str) -> CatalogModel:
        return replace(self, source_id=source_id)


@dataclass(frozen=True, slots=True)
class CatalogQuarantine:
    source_id: str
    reason: QuarantineReason
    source_sha256: str
    preserved_visible_row_sha256: str | None


@dataclass(frozen=True, slots=True)
class CatalogSources:
    zen: ZenSnapshot
    models_dev: ModelsDevSnapshot
    official: OfficialSnapshot


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    status: ObservationStatus
    observed_at: datetime
    complete: bool
    source_ids: tuple[str, ...]
    accepted_ids: tuple[str, ...]
    accepted_models: tuple[CatalogModel, ...]
    preserved_models: tuple[CatalogModel, ...]
    quarantine: tuple[CatalogQuarantine, ...]
    source_ids_sha256: str
    accepted_ids_sha256: str
    decision_sha256: str
    provenance: tuple[SourceProvenance, ...]

    @property
    def visible_models(self) -> tuple[CatalogModel, ...]:
        return tuple(
            sorted(
                (*self.accepted_models, *self.preserved_models),
                key=lambda item: item.source_id,
            )
        )
