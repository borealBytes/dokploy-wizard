# pyright: reportMissingImports=false

from __future__ import annotations

import json
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


def test_list_keys_preserves_metadata_from_payload() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "items": [
                {
                    "key": "key-1",
                    "key_alias": "alias-1",
                    "team_id": "team-1",
                    "models": ["tuxdesktop.tailb12aa5.ts.net/unsloth-active"],
                    "metadata": {"consumer": "alias-1", "managed_by": "dokploy-wizard"},
                }
            ]
        }

    keys = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).list_keys()

    assert keys[0].metadata == {"consumer": "alias-1", "managed_by": "dokploy-wizard"}


def test_update_team_posts_models_and_metadata() -> None:
    recorded: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        recorded["url"] = req.full_url
        recorded["method"] = req.get_method()
        recorded["headers"] = dict(req.header_items())
        raw_body = req.data
        assert isinstance(raw_body, bytes)
        recorded["body"] = json.loads(raw_body.decode("utf-8"))
        return {
            "team_id": "team-my-farm-advisor",
            "team_alias": "my-farm-advisor",
            "models": [
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ],
            "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
        }

    team = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).update_team(
        team_id="team-my-farm-advisor",
        team_alias="my-farm-advisor",
        models=(
            "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            "openrouter/anthropic/claude-3.5-sonnet",
        ),
        metadata={"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
    )

    assert recorded["url"] == "http://litellm.internal/team/update"
    assert recorded["method"] == "POST"
    assert recorded["body"] == {
        "team_id": "team-my-farm-advisor",
        "team_alias": "my-farm-advisor",
        "models": [
            "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            "openrouter/anthropic/claude-3.5-sonnet",
        ],
        "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
    }
    assert team.metadata == {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"}


def test_create_team_accepts_enveloped_team_response() -> None:
    def fake_request(req: request.Request) -> object:
        assert req.full_url == "http://litellm.internal/team/new"
        assert req.get_method() == "POST"
        raw_body = req.data
        assert isinstance(raw_body, bytes)
        assert json.loads(raw_body.decode("utf-8")) == {
            "team_alias": "my-farm-advisor",
            "models": ["openrouter/anthropic/claude-3.5-sonnet"],
            "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
        }
        return {
            "ok": True,
            "team": {
                "team_id": "team-my-farm-advisor",
                "team_alias": "my-farm-advisor",
                "models": ["openrouter/anthropic/claude-3.5-sonnet"],
                "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
            },
        }

    team = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).create_team(
        team_alias="my-farm-advisor",
        models=("openrouter/anthropic/claude-3.5-sonnet",),
        metadata={"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
    )

    assert team.team_id == "team-my-farm-advisor"
    assert team.team_alias == "my-farm-advisor"
    assert team.models == ("openrouter/anthropic/claude-3.5-sonnet",)


def test_create_team_accepts_aliasless_enveloped_team_response_with_fallback() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "data": {
                "models": ["openrouter/anthropic/claude-3.5-sonnet"],
                "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
            }
        }

    team = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).create_team(
        team_alias="my-farm-advisor",
        models=("openrouter/anthropic/claude-3.5-sonnet",),
        metadata={"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
    )

    assert team.team_id == "my-farm-advisor"
    assert team.team_alias == "my-farm-advisor"
    assert team.models == ("openrouter/anthropic/claude-3.5-sonnet",)
    assert team.metadata == {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"}


def test_update_team_accepts_enveloped_team_info_response() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "message": "updated",
            "team_info": {
                "team_id": "team-my-farm-advisor",
                "team_alias": "my-farm-advisor",
                "models": ["tuxdesktop.tailb12aa5.ts.net/unsloth-active"],
                "metadata": {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
            },
        }

    team = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).update_team(
        team_id="team-my-farm-advisor",
        team_alias="my-farm-advisor",
        models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
        metadata={"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"},
    )

    assert team.team_id == "team-my-farm-advisor"
    assert team.team_alias == "my-farm-advisor"
    assert team.models == ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)


def test_update_team_accepts_aliasless_direct_team_response_with_fallbacks() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "team_id": "team-openclaw",
            "models": ["tuxdesktop.tailb12aa5.ts.net/unsloth-active"],
            "metadata": {"consumer": "openclaw", "managed_by": "dokploy-wizard"},
        }

    team = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).update_team(
        team_id="team-openclaw",
        team_alias="openclaw",
        models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
        metadata={"consumer": "openclaw", "managed_by": "dokploy-wizard"},
    )

    assert team.team_id == "team-openclaw"
    assert team.team_alias == "openclaw"
    assert team.models == ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)
    assert team.metadata == {"consumer": "openclaw", "managed_by": "dokploy-wizard"}


def test_update_team_fails_actionably_for_malformed_envelope() -> None:
    def fake_request(_: request.Request) -> object:
        return {"team": {"status": "updated"}}

    client = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    )

    with pytest.raises(
        LiteLLMAdminError,
        match=r"team\.update response missing required string field.*nested objects under",
    ):
        client.update_team(
            team_id="team-my-farm-advisor",
            team_alias="my-farm-advisor",
            models=("openrouter/anthropic/claude-3.5-sonnet",),
        )


def test_create_key_accepts_enveloped_generated_key_response() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "info": "created",
            "data": {
                "key": "sk-generated",
                "key_alias": "openclaw",
                "team_id": "team-openclaw",
                "models": ["openrouter/anthropic/claude-3.5-sonnet"],
                "metadata": {"consumer": "openclaw", "managed_by": "dokploy-wizard"},
            },
        }

    key = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).create_key(
        key="sk-generated",
        key_alias="openclaw",
        team_id="team-openclaw",
        models=("openrouter/anthropic/claude-3.5-sonnet",),
        metadata={"consumer": "openclaw", "managed_by": "dokploy-wizard"},
    )

    assert key.key == "sk-generated"
    assert key.key_alias == "openclaw"
    assert key.team_id == "team-openclaw"
    assert key.models == ("openrouter/anthropic/claude-3.5-sonnet",)


def test_update_key_accepts_nested_key_record_with_fallback_fields() -> None:
    def fake_request(_: request.Request) -> object:
        return {
            "result": {
                "models": ["tuxdesktop.tailb12aa5.ts.net/unsloth-active"],
                "metadata": {"consumer": "openclaw", "managed_by": "dokploy-wizard"},
            }
        }

    key = LiteLLMAdminClient(
        api_url="http://litellm.internal",
        master_key="secret",
        request_fn=fake_request,
    ).update_key(
        key_alias="openclaw",
        key="sk-existing",
        team_id="team-openclaw",
        models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
        metadata={"consumer": "openclaw", "managed_by": "dokploy-wizard"},
    )

    assert key.key == "sk-existing"
    assert key.key_alias == "openclaw"
    assert key.team_id == "team-openclaw"
    assert key.models == ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)
