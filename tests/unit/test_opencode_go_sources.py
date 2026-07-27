from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from dokploy_wizard.litellm.catalog_sources import (
    MODELS_DEV_SOURCE,
    OFFICIAL_SOURCE,
    ZEN_SOURCE,
    FetchRequest,
    FetchResponse,
    SourceContractError,
    fetch_source,
    parse_models_dev,
    parse_official,
    parse_zen,
)
from tests.unit._opencode_go_catalog_support import StaticClock


@dataclass(frozen=True, slots=True)
class RecordingTransport:
    response: FetchResponse
    requests: list[tuple[str, str, tuple[tuple[str, str], ...], float, bool]]

    def execute(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(
            (
                request.method,
                request.url,
                request.headers,
                request.timeout_seconds,
                request.follow_redirects,
            )
        )
        return self.response


def _response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
) -> FetchResponse:
    return FetchResponse(status=status, content_type=content_type, chunks=(body,))


def test_fetch_contract_uses_only_fixed_public_headers_and_no_redirects() -> None:
    transport = RecordingTransport(_response(b'{"object":"list","data":[]}'), [])

    fetch_source(ZEN_SOURCE, transport)

    assert transport.requests == [
        (
            "GET",
            "https://opencode.ai/zen/go/v1/models",
            (("Accept", "application/json"), ("User-Agent", "dokploy-wizard-model-sync/1")),
            30.0,
            False,
        )
    ]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_response(b"{}", status=302), "status"),
        (_response(b"{}", status=401), "status"),
        (_response(b"{}", content_type="text/html"), "content_type"),
    ],
)
def test_fetch_contract_rejects_redirect_auth_and_wrong_content_type(
    response: FetchResponse, reason: str
) -> None:
    with pytest.raises(SourceContractError, match=reason):
        fetch_source(ZEN_SOURCE, RecordingTransport(response, []))


def test_fetch_contract_stream_enforces_source_limit() -> None:
    response = FetchResponse(200, "application/json", (b"a" * ZEN_SOURCE.max_bytes, b"x"))

    with pytest.raises(SourceContractError, match="size"):
        fetch_source(ZEN_SOURCE, RecordingTransport(response, []))


def test_zen_parser_requires_strict_unique_model_records() -> None:
    payload = {
        "object": "list",
        "data": [
            {"id": "model-a", "object": "model", "created": 1, "owned_by": "opencode"},
            {"id": "model-b", "object": "model", "created": 2, "owned_by": "opencode"},
        ],
    }
    document = fetch_source(
        ZEN_SOURCE,
        RecordingTransport(_response(json.dumps(payload).encode()), []),
    )

    snapshot = parse_zen(document, StaticClock())

    assert snapshot.ids == ("model-a", "model-b")
    assert snapshot.provenance.raw_sha256 == hashlib.sha256(document.body).hexdigest()


def test_models_dev_parser_rejects_duplicate_provider_keys() -> None:
    body = b'{"opencode-go":{"id":"opencode-go","npm":"x","models":{}},"opencode-go":{}}'
    document = fetch_source(MODELS_DEV_SOURCE, RecordingTransport(_response(body), []))

    with pytest.raises(SourceContractError, match="duplicate"):
        parse_models_dev(document, StaticClock())


def test_models_dev_retains_invalid_pricing_and_limits_for_quarantine() -> None:
    payload = {
        "opencode-go": {
            "id": "opencode-go",
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "bad-price": {
                    "id": "bad-price",
                    "name": "Bad Price",
                    "cost": {"input": 1, "output": 2, "cache_read": -1},
                    "limit": {"context": 100, "output": 10},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "bad-limit": {
                    "id": "bad-limit",
                    "name": "Bad Limit",
                    "cost": {"input": 1, "output": 2},
                    "limit": {"context": 100, "output": 0},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
            },
        }
    }
    document = fetch_source(
        MODELS_DEV_SOURCE,
        RecordingTransport(_response(json.dumps(payload).encode()), []),
    )

    snapshot = parse_models_dev(document, StaticClock())

    bad_price = snapshot.model_for("bad-price")
    bad_limit = snapshot.model_for("bad-limit")
    assert bad_price is not None
    assert bad_limit is not None
    assert bad_price.pricing_valid is False
    assert bad_limit.limits_valid is False


def test_official_hash_mismatch_rejects_entire_source() -> None:
    source = OFFICIAL_SOURCE.with_expected_sha256("0" * 64)

    with pytest.raises(SourceContractError, match="hash"):
        fetch_source(
            source,
            RecordingTransport(_response(b"invalid", content_type="text/plain"), []),
        )


def test_official_pin_and_minimax_rows_are_exact() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "opencode-go-mixed-catalog-v1.json").read_text()
    )
    pricing_lines = ["| " + " | ".join(row) + " |" for row in fixture["official_pricing"]]
    endpoint_lines = ["| " + " | ".join(row) + " |" for row in fixture["official_endpoints"]]
    raw = ("\n".join([
        "| Model | Input | Output | Cached Read | Cached Write | Usage |",
        "| --- | --- | --- | --- | --- | --- |",
        *pricing_lines,
        "| Model | Model ID | Endpoint | AI SDK Package |",
        "| --- | --- | --- | --- |",
        *endpoint_lines,
    ]) + "\n").encode()
    source = OFFICIAL_SOURCE.with_expected_sha256(hashlib.sha256(raw).hexdigest())
    document = fetch_source(
        source,
        RecordingTransport(_response(raw, content_type="text/plain"), []),
    )

    snapshot = parse_official(document, StaticClock())

    rows = {row.model_name: row.row_sha256 for row in snapshot.pricing_rows}
    assert OFFICIAL_SOURCE.expected_sha256 == (
        "20a8a45e3fb78c93a7f143a9a4c0ec8b011209065e215d24a68d298f313f96d3"
    )
    assert rows["MiniMax M2.7"] == (
        "bce5766abf4bea902e36fd6665ba5a5f5768e78973fb5b71a2675da84ce307d6"
    )
    assert rows["MiniMax M2.5"] == (
        "c35b109afbe6316f2bdd80ebd6002933d4e53862bea969ba1f99ed50caf3d5c1"
    )
