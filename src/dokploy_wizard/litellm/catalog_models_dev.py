from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeGuard

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_types import (
    Modalities,
    ModelLimits,
    PriceDimensions,
    PriceTier,
    SourceContractError,
    SourcePricing,
)


@dataclass(frozen=True, slots=True)
class ParsedModelFields:
    name: str
    pricing: SourcePricing | None
    pricing_valid: bool
    pricing_payload: JsonValue
    limits: ModelLimits | None
    limits_valid: bool
    limits_payload: JsonValue
    modalities: Modalities
    modalities_payload: JsonValue


def parse_model_fields(record: dict[str, JsonValue]) -> ParsedModelFields:
    name = _text(record.get("name"), "Models.dev model name")
    pricing, pricing_valid, pricing_payload = _pricing(record.get("cost"))
    limits, limits_valid, limits_payload = _limits(record.get("limit"))
    modalities, modalities_payload = _modalities(record.get("modalities"))
    return ParsedModelFields(
        name,
        pricing,
        pricing_valid,
        pricing_payload,
        limits,
        limits_valid,
        limits_payload,
        modalities,
        modalities_payload,
    )


def _pricing(value: JsonValue) -> tuple[SourcePricing | None, bool, JsonValue]:
    if not isinstance(value, dict):
        return None, False, None
    base, fields_valid = _dimensions(value)
    tier_values = value.get("tiers", [])
    if not isinstance(tier_values, list):
        return None, False, None
    tiers: list[PriceTier] = []
    tier_payloads: list[JsonValue] = []
    for index, tier_value in enumerate(tier_values):
        if not isinstance(tier_value, dict):
            return None, False, None
        tier = tier_value.get("tier")
        tier_name = f"tier_{index}"
        if isinstance(tier, dict) and _positive_integer(tier.get("size")):
            tier_name = f"context_over_{tier['size']}"
        dimensions, tier_valid = _dimensions(tier_value)
        fields_valid = fields_valid and tier_valid
        tiers.append(PriceTier(tier_name, dimensions))
        tier_payloads.append(
            {"dimensions": _dimensions_payload(dimensions), "name": tier_name}
        )
    result = SourcePricing(base, tuple(tiers))
    valid = (
        fields_valid
        and _dimensions_required(base)
        and all(_dimensions_required(item.dimensions) for item in tiers)
    )
    return result, valid, {"base": _dimensions_payload(base), "tiers": tier_payloads}


def _limits(value: JsonValue) -> tuple[ModelLimits | None, bool, JsonValue]:
    if not isinstance(value, dict):
        return None, False, None
    context = value.get("context")
    output = value.get("output")
    payload: JsonValue = {"context": context, "output": output}
    if not _positive_integer(context):
        return None, False, payload
    if not _positive_integer(output):
        return None, False, payload
    return ModelLimits(context, output), True, payload


def _modalities(value: JsonValue) -> tuple[Modalities, JsonValue]:
    mapping = _mapping(value, "Models.dev modalities")
    inputs = tuple(
        _text(item, "Models.dev input modality")
        for item in _list(mapping.get("input"), "modalities input")
    )
    outputs = tuple(
        _text(item, "Models.dev output modality")
        for item in _list(mapping.get("output"), "modalities output")
    )
    return Modalities(inputs, outputs), {"input": list(inputs), "output": list(outputs)}


def _dimensions(value: dict[str, JsonValue]) -> tuple[PriceDimensions, bool]:
    parsed = tuple(
        _dimension(value, key)
        for key in ("input", "output", "cache_read", "cache_write")
    )
    dimensions = PriceDimensions(*(item[0] for item in parsed))
    return dimensions, all(item[1] for item in parsed)


def _dimension(value: dict[str, JsonValue], key: str) -> tuple[float | None, bool]:
    if key not in value or value[key] is None:
        return None, True
    raw = value[key]
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None, False
    parsed = float(raw)
    if not math.isfinite(parsed) or parsed < 0:
        return None, False
    return parsed, True


def _dimensions_required(value: PriceDimensions) -> bool:
    return value.input is not None and value.output is not None


def _dimensions_payload(value: PriceDimensions) -> JsonValue:
    return {
        "cache_read": value.cache_read,
        "cache_write": value.cache_write,
        "input": value.input,
        "output": value.output,
    }


def _positive_integer(value: JsonValue) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise SourceContractError(f"{label} must be an object")
    return value


def _list(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise SourceContractError(f"{label} must be a list")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceContractError(f"{label} must be text")
    return value.strip()
