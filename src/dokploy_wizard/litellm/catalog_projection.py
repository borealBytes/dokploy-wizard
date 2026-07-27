from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ZenModelProjection:
    url: str
    snapshot_sha256: str
    row_sha256: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ModelsDevModelProjection:
    url: str
    snapshot_sha256: str
    row_sha256: str
    pricing_row_sha256: str
    provider_npm: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OfficialModelProjection:
    url: str
    commit: str
    blob_sha256: str
    snapshot_sha256: str
    row_sha256: str | None


@dataclass(frozen=True, slots=True)
class ModelDecisionProjection:
    zen: ZenModelProjection
    models_dev: ModelsDevModelProjection
    official_transport: OfficialModelProjection
    official_pricing: OfficialModelProjection
