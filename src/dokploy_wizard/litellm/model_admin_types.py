from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NewType, Protocol

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_types import PriceProvenance, TransportName

ModelUuid = NewType("ModelUuid", str)
PricingZero = Literal["not_zero", "explicit_zero", "input_fallback_from_explicit_zero"]


class LiteLLMModelAdminError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


class LiteLLMModelAdminConflict(LiteLLMModelAdminError):
    pass


class LiteLLMModelAdminWriteAmbiguity(LiteLLMModelAdminError):
    pass


@dataclass(frozen=True, slots=True)
class LiteLLMRoutingParams:
    model: str
    api_base: str
    api_key: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"model": self.model, "api_base": self.api_base, "api_key": self.api_key}


@dataclass(frozen=True, slots=True)
class LiteLLMInventoryRoutingParams:
    model: str
    api_base: str
    api_key: str | None

    @classmethod
    def from_owned(cls, routing: LiteLLMRoutingParams) -> LiteLLMInventoryRoutingParams:
        return cls(model=routing.model, api_base=routing.api_base, api_key=routing.api_key)


@dataclass(frozen=True, slots=True)
class LiteLLMSourceProjection:
    zen_url: str
    zen_sha256: str
    zen_observed_at: str
    models_dev_url: str
    models_dev_sha256: str
    models_dev_pricing_row_sha256: str
    models_dev_provider_npm: str
    models_dev_observed_at: str
    official_transport_url: str
    official_transport_commit: str
    official_transport_blob_sha256: str
    official_transport_row_sha256: str
    official_pricing_url: str
    official_pricing_commit: str
    official_pricing_blob_sha256: str
    official_pricing_row_sha256: str


@dataclass(frozen=True, slots=True)
class LiteLLMPriceDimensions:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True, slots=True)
class LiteLLMPricingProvenance:
    selection: PriceProvenance
    selected_per_million: float
    zero: PricingZero
    tiers_sha256: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "selection": self.selection,
            "selected_per_million": self.selected_per_million,
            "zero": self.zero,
            "tiers_sha256": self.tiers_sha256,
        }


@dataclass(frozen=True, slots=True)
class LiteLLMPricing:
    costs_per_token: LiteLLMPriceDimensions
    source_per_million: LiteLLMPriceDimensions
    scalar_per_million: LiteLLMPriceDimensions
    input_provenance: LiteLLMPricingProvenance
    output_provenance: LiteLLMPricingProvenance
    cache_read_provenance: LiteLLMPricingProvenance
    cache_write_provenance: LiteLLMPricingProvenance
    tiers_sha256: str
    selection_sha256: str


@dataclass(frozen=True, slots=True)
class LiteLLMOwnedModelInfo:
    model_id: ModelUuid
    source_id: str
    transport: TransportName
    source: LiteLLMSourceProjection
    pricing: LiteLLMPricing
    merged_decision_sha256: str
    max_input_tokens: int
    max_output_tokens: int
    bootstrap_static: bool
    spec_sha256: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": self.model_id,
            "blocked": False,
            "mode": "chat",
            "managed_by": "dokploy-wizard",
            "managed_catalog": "opencode-go",
            "source_id": self.source_id,
            "transport": self.transport,
            "zen_url": self.source.zen_url,
            "zen_sha256": self.source.zen_sha256,
            "zen_observed_at": self.source.zen_observed_at,
            "models_dev_url": self.source.models_dev_url,
            "models_dev_sha256": self.source.models_dev_sha256,
            "models_dev_pricing_row_sha256": self.source.models_dev_pricing_row_sha256,
            "models_dev_provider_npm": self.source.models_dev_provider_npm,
            "models_dev_observed_at": self.source.models_dev_observed_at,
            "official_transport_url": self.source.official_transport_url,
            "official_transport_commit": self.source.official_transport_commit,
            "official_transport_blob_sha256": self.source.official_transport_blob_sha256,
            "official_transport_row_sha256": self.source.official_transport_row_sha256,
            "official_pricing_url": self.source.official_pricing_url,
            "official_pricing_commit": self.source.official_pricing_commit,
            "official_pricing_blob_sha256": self.source.official_pricing_blob_sha256,
            "official_pricing_row_sha256": self.source.official_pricing_row_sha256,
            "merged_decision_sha256": self.merged_decision_sha256,
            "input_cost_per_token": self.pricing.costs_per_token.input,
            "output_cost_per_token": self.pricing.costs_per_token.output,
            "cache_read_input_token_cost": self.pricing.costs_per_token.cache_read,
            "cache_creation_input_token_cost": self.pricing.costs_per_token.cache_write,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "dokploy_source_input_per_million": self.pricing.source_per_million.input,
            "dokploy_source_output_per_million": self.pricing.source_per_million.output,
            "dokploy_source_cache_read_per_million": self.pricing.source_per_million.cache_read,
            "dokploy_source_cache_write_per_million": self.pricing.source_per_million.cache_write,
            "dokploy_scalar_input_per_million": self.pricing.scalar_per_million.input,
            "dokploy_scalar_output_per_million": self.pricing.scalar_per_million.output,
            "dokploy_scalar_cache_read_per_million": self.pricing.scalar_per_million.cache_read,
            "dokploy_scalar_cache_write_per_million": self.pricing.scalar_per_million.cache_write,
            "dokploy_pricing_tiers_sha256": self.pricing.tiers_sha256,
            "dokploy_pricing_selection_sha256": self.pricing.selection_sha256,
            "dokploy_pricing_input_provenance": self.pricing.input_provenance.to_json(),
            "dokploy_pricing_output_provenance": self.pricing.output_provenance.to_json(),
            "dokploy_pricing_cache_read_provenance": self.pricing.cache_read_provenance.to_json(),
            "dokploy_pricing_cache_write_provenance": self.pricing.cache_write_provenance.to_json(),
            "dokploy_spec_sha256": self.spec_sha256,
            "bootstrap_static": self.bootstrap_static,
        }

    def to_unhashed_json(self) -> dict[str, JsonValue]:
        payload = self.to_json()
        del payload["dokploy_spec_sha256"]
        return payload


@dataclass(frozen=True, slots=True)
class LiteLLMModelDeployment:
    model_name: str
    litellm_params: LiteLLMRoutingParams
    model_info: LiteLLMOwnedModelInfo
    server_model_info_extras: tuple[tuple[str, JsonValue], ...] = ()

    @property
    def model_id(self) -> ModelUuid:
        return self.model_info.model_id

    def to_api_payload(self) -> dict[str, JsonValue]:
        model_info = self.model_info.to_json()
        model_info.update(self.server_model_info_extras)
        return {
            "model_name": self.model_name,
            "litellm_params": self.litellm_params.to_json(),
            "model_info": model_info,
        }


@dataclass(frozen=True, slots=True)
class LiteLLMModelRecord:
    model_id: ModelUuid
    model_name: str
    litellm_params: LiteLLMInventoryRoutingParams
    model_info: dict[str, JsonValue]
    server_model_info_extras: dict[str, JsonValue]


class LiteLLMModelAdminApi(Protocol):
    def list_models(self) -> tuple[LiteLLMModelRecord, ...]: ...

    def create_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord: ...

    def update_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord: ...

    def delete_model(self, model_id: ModelUuid) -> None: ...
