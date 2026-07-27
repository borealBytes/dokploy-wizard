from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

SourceName = Literal["zen", "models_dev", "official"]
TransportName = Literal["openai", "anthropic"]
PriceProvenance = Literal["max_both", "official_only", "models_dev_only", "input_fallback"]
QuarantineReason = Literal["missing_official_transport", "invalid_pricing", "invalid_limits"]
ObservationStatus = Literal[
    "accepted",
    "accepted_with_quarantine",
    "rejected_invalid",
    "quarantined_anomalous",
    "rejected_clock_regression",
]


class SourceContractError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: SourceName
    url: str
    max_bytes: int
    accepted_content_types: tuple[str, ...]
    expected_sha256: str | None = None
    commit: str | None = None
    blob: str | None = None

    def with_expected_sha256(self, expected_sha256: str) -> SourceSpec:
        return replace(self, expected_sha256=expected_sha256)


@dataclass(frozen=True, slots=True)
class FetchRequest:
    method: Literal["GET"]
    url: str
    headers: tuple[tuple[str, str], ...]
    timeout_seconds: float
    follow_redirects: Literal[False]
    max_bytes: int


@dataclass(frozen=True, slots=True)
class FetchResponse:
    status: int
    content_type: str
    chunks: tuple[bytes, ...]


class SourceTransport(Protocol):
    def execute(self, request: FetchRequest) -> FetchResponse: ...


@dataclass(frozen=True, slots=True)
class FetchDocument:
    spec: SourceSpec
    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    source: SourceName
    url: str
    content_type: str
    byte_count: int
    raw_sha256: str
    projected_sha256: str
    commit: str | None
    blob: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PriceDimensions:
    input: float | None
    output: float | None
    cache_read: float | None
    cache_write: float | None


@dataclass(frozen=True, slots=True)
class PriceTier:
    name: str
    dimensions: PriceDimensions


@dataclass(frozen=True, slots=True)
class SourcePricing:
    base: PriceDimensions
    tiers: tuple[PriceTier, ...]


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    value: float
    provenance: PriceProvenance


@dataclass(frozen=True, slots=True)
class ResolvedPriceTier:
    name: str
    input: ResolvedPrice
    output: ResolvedPrice
    cache_read: ResolvedPrice
    cache_write: ResolvedPrice


@dataclass(frozen=True, slots=True)
class ResolvedPricing:
    tiers: tuple[ResolvedPriceTier, ...]
    scalar_input_per_token: float
    scalar_output_per_token: float
    scalar_cache_read_per_token: float
    scalar_cache_write_per_token: float


@dataclass(frozen=True, slots=True)
class ModelLimits:
    context: int
    output: int


@dataclass(frozen=True, slots=True)
class Modalities:
    input: tuple[str, ...]
    output: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ZenSnapshot:
    ids: tuple[str, ...]
    row_sha256_by_id: tuple[tuple[str, str], ...]
    provenance: SourceProvenance

    def row_sha256_for(self, source_id: str) -> str | None:
        return next(
            (
                digest
                for model_id, digest in self.row_sha256_by_id
                if model_id == source_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class ModelsDevModel:
    source_id: str
    name: str
    pricing: SourcePricing | None
    pricing_valid: bool
    limits: ModelLimits | None
    limits_valid: bool
    modalities: Modalities
    projected_row_sha256: str
    pricing_row_sha256: str


@dataclass(frozen=True, slots=True)
class ModelsDevSnapshot:
    provider_id: str
    provider_npm: str
    models: tuple[ModelsDevModel, ...]
    provenance: SourceProvenance

    def model_for(self, source_id: str) -> ModelsDevModel | None:
        return next((model for model in self.models if model.source_id == source_id), None)


@dataclass(frozen=True, slots=True)
class OfficialEndpoint:
    model_name: str
    source_id: str
    endpoint: str
    package: str
    row_sha256: str


@dataclass(frozen=True, slots=True)
class OfficialPriceRow:
    model_name: str
    base_model_name: str
    tier_name: str
    dimensions: PriceDimensions
    usage_limit: float
    row_sha256: str


@dataclass(frozen=True, slots=True)
class OfficialSnapshot:
    endpoints: tuple[OfficialEndpoint, ...]
    pricing_rows: tuple[OfficialPriceRow, ...]
    provenance: SourceProvenance

