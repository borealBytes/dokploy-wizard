from __future__ import annotations

import math
import re
from datetime import datetime

from dokploy_wizard.litellm.catalog_json import JsonValue, canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_types import (
    FetchDocument,
    OfficialEndpoint,
    OfficialPriceRow,
    OfficialSnapshot,
    PriceDimensions,
    PriceTier,
    SourcePricing,
    SourceProvenance,
)

_PRICING_HEADER = ("Model", "Input", "Output", "Cached Read", "Cached Write", "Usage")
_ENDPOINT_HEADER = ("Model", "Model ID", "Endpoint", "AI SDK Package")
_TIER_SUFFIX = re.compile(r"\s*\(([^()]*)\)\s*$")


class OfficialContractError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


def parse_official_document(document: FetchDocument, observed_at: datetime) -> OfficialSnapshot:
    try:
        text = document.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OfficialContractError("official source is not UTF-8") from error
    pricing_cells = _table_rows(text, _PRICING_HEADER)
    endpoint_cells = _table_rows(text, _ENDPOINT_HEADER)
    pricing_rows = tuple(_price_row(cells) for cells in pricing_cells)
    endpoints = tuple(_endpoint_row(cells) for cells in endpoint_cells)
    if len({row.model_name for row in pricing_rows}) != len(pricing_rows):
        raise OfficialContractError("official pricing rows are not unique")
    if len({row.source_id for row in endpoints}) != len(endpoints):
        raise OfficialContractError("official endpoint rows are not unique")
    projected: JsonValue = {
        "endpoints": [_endpoint_payload(row) for row in endpoints],
        "pricing": [_pricing_payload(row) for row in pricing_rows],
    }
    provenance = SourceProvenance(
        source="official",
        url=document.spec.url,
        content_type=document.content_type,
        byte_count=len(document.body),
        raw_sha256=sha256_bytes(document.body),
        projected_sha256=sha256_bytes(canonical_json_bytes(projected)),
        commit=document.spec.commit,
        blob=document.spec.blob,
        observed_at=observed_at,
    )
    return OfficialSnapshot(endpoints, pricing_rows, provenance)


def official_pricing_for(snapshot: OfficialSnapshot, model_name: str) -> SourcePricing | None:
    rows = official_pricing_rows_for(snapshot, model_name)
    if not rows:
        return None
    base_row = next((row for row in rows if row.tier_name == "base"), rows[0])
    tiers = tuple(
        PriceTier(row.tier_name, row.dimensions)
        for row in rows
        if row.tier_name != "base" or len(rows) > 1
    )
    return SourcePricing(base_row.dimensions, tiers)


def official_pricing_rows_for(
    snapshot: OfficialSnapshot,
    model_name: str,
) -> tuple[OfficialPriceRow, ...]:
    key = _model_key(model_name)
    return tuple(
        row for row in snapshot.pricing_rows if _model_key(row.base_model_name) == key
    )


def _table_rows(text: str, header: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if _split_row(line) == header]
    if len(matches) != 1:
        raise OfficialContractError(f"official {header[0].lower()} table header is not unique")
    rows: list[tuple[str, ...]] = []
    for line in lines[matches[0] + 2 :]:
        cells = _split_row(line)
        if not cells:
            break
        if cells in {_PRICING_HEADER, _ENDPOINT_HEADER}:
            break
        if len(cells) != len(header):
            raise OfficialContractError("official table row width is invalid")
        rows.append(cells)
    if not rows:
        raise OfficialContractError("official table has no rows")
    return tuple(rows)


def _split_row(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip().replace("≤", "<=") for cell in stripped[1:-1].split("|"))


def _price_row(cells: tuple[str, ...]) -> OfficialPriceRow:
    model_name, input_value, output, cache_read, cache_write, usage = cells
    tier_match = _TIER_SUFFIX.search(model_name)
    base_name = model_name if tier_match is None else model_name[: tier_match.start()].strip()
    tier_name = "base" if tier_match is None else _tier_name(tier_match.group(1))
    dimensions = PriceDimensions(
        _money(input_value, required=True),
        _money(output, required=True),
        _money(cache_read, required=False),
        _money(cache_write, required=False),
    )
    usage_limit = _money(usage, required=True)
    if usage_limit is None:
        raise OfficialContractError("official usage limit is missing")
    return OfficialPriceRow(
        model_name,
        base_name,
        tier_name,
        dimensions,
        usage_limit,
        sha256_bytes(canonical_json_bytes(list(cells))),
    )


def _endpoint_row(cells: tuple[str, ...]) -> OfficialEndpoint:
    model_name, source_id, endpoint, package = cells
    endpoint = endpoint.strip("`")
    package = package.strip("`")
    return OfficialEndpoint(
        model_name,
        source_id,
        endpoint,
        package,
        sha256_bytes(canonical_json_bytes(list(cells))),
    )


def _money(value: str, *, required: bool) -> float | None:
    if value == "-":
        if required:
            raise OfficialContractError("required official price is missing")
        return None
    if not value.startswith("$"):
        raise OfficialContractError("official price format is invalid")
    try:
        parsed = float(value[1:].replace(",", ""))
    except ValueError as error:
        raise OfficialContractError("official price format is invalid") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise OfficialContractError("official price is invalid")
    return parsed


def _tier_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _endpoint_payload(row: OfficialEndpoint) -> JsonValue:
    return {
        "endpoint": row.endpoint,
        "model_name": row.model_name,
        "package": row.package,
        "row_sha256": row.row_sha256,
        "source_id": row.source_id,
    }


def _pricing_payload(row: OfficialPriceRow) -> JsonValue:
    return {
        "base_model_name": row.base_model_name,
        "dimensions": _dimensions_payload(row.dimensions),
        "model_name": row.model_name,
        "row_sha256": row.row_sha256,
        "tier_name": row.tier_name,
        "usage_limit": row.usage_limit,
    }


def _dimensions_payload(value: PriceDimensions) -> JsonValue:
    return {
        "cache_read": value.cache_read,
        "cache_write": value.cache_write,
        "input": value.input,
        "output": value.output,
    }
