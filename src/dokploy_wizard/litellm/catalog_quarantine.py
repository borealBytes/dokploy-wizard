from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_model_decision import catalog_model_payload
from dokploy_wizard.litellm.catalog_observation_types import (
    CatalogModel,
    CatalogQuarantine,
    CatalogSources,
)
from dokploy_wizard.litellm.catalog_types import (
    ModelsDevModel,
    OfficialEndpoint,
    QuarantineReason,
)


@dataclass(frozen=True, slots=True)
class QuarantineCandidate:
    source_id: str
    reason: QuarantineReason
    endpoint: OfficialEndpoint | None
    model: ModelsDevModel | None
    previous_visible: CatalogModel | None


def quarantine_model(
    candidate: QuarantineCandidate,
    sources: CatalogSources,
) -> tuple[CatalogModel | None, CatalogQuarantine]:
    previous_visible = candidate.previous_visible
    preserved_visible_row_sha256 = (
        None
        if previous_visible is None
        else sha256_bytes(canonical_json_bytes(catalog_model_payload(previous_visible)))
    )
    return previous_visible, CatalogQuarantine(
        candidate.source_id,
        candidate.reason,
        _evidence_sha256(candidate, sources),
        preserved_visible_row_sha256,
    )


def _evidence_sha256(
    candidate: QuarantineCandidate,
    sources: CatalogSources,
) -> str:
    match candidate.reason:
        case "missing_official_transport":
            return (
                sources.official.provenance.projected_sha256
                if candidate.endpoint is None
                else candidate.endpoint.row_sha256
            )
        case "invalid_pricing":
            return (
                sources.models_dev.provenance.projected_sha256
                if candidate.model is None
                else candidate.model.pricing_row_sha256
            )
        case "invalid_limits":
            return (
                sources.models_dev.provenance.projected_sha256
                if candidate.model is None
                else candidate.model.projected_row_sha256
            )
        case unreachable:
            assert_never(unreachable)
