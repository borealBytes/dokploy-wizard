# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass
from urllib import parse, request

import pytest

from dokploy_wizard.litellm.admin import LiteLLMAdminClient, LiteLLMAdminError


@dataclass
class _RecordedRequest:
    url: str


def test_list_keys_uses_page_size_at_most_100_and_paginates_lists() -> None:
    recorded: list[_RecordedRequest] = []

    def fake_request(req: request.Request) -> object:
        recorded.append(_RecordedRequest(url=req.full_url))
        query = parse.parse_qs(parse.urlparse(req.full_url).query)
        page = int(query["page"][0])
        size = int(query["size"][0])
        assert size == 100
        if page == 1:
            return [
                {
                    "key": f"key-{index}",
                    "key_alias": f"alias-{index}",
                    "team_id": "team-1",
                    "models": ["openai/*"],
                }
                for index in range(100)
            ]
        if page == 2:
            return [
                {
                    "key": "key-100",
                    "key_alias": "alias-100",
                    "team_id": "team-1",
                    "models": ["openai/*"],
                }
            ]
        raise AssertionError(f"unexpected page {page}")

    client = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    )

    keys = client.list_keys()

    assert len(keys) == 101
    pages = [parse.parse_qs(parse.urlparse(item.url).query)["page"][0] for item in recorded]
    sizes = [parse.parse_qs(parse.urlparse(item.url).query)["size"][0] for item in recorded]
    assert pages == ["1", "2"]
    assert sizes == ["100", "100"]


def test_list_keys_accepts_paginated_object_payload() -> None:
    def fake_request(req: request.Request) -> object:
        query = parse.parse_qs(parse.urlparse(req.full_url).query)
        page = int(query["page"][0])
        if page == 1:
            return {
                "items": [
                    {
                        "key": "key-1",
                        "key_alias": "alias-1",
                        "team_id": "team-1",
                        "models": ["openai/*"],
                    }
                ]
            }
        return {"items": []}

    keys = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).list_keys()

    assert len(keys) == 1
    assert keys[0].key_alias == "alias-1"


def test_list_keys_fails_actionably_for_unrecognized_paginated_object() -> None:
    def fake_request(_: request.Request) -> object:
        return {"unexpected": []}

    client = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    )

    with pytest.raises(LiteLLMAdminError, match="must contain a list under one of"):
        client.list_keys()
