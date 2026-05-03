# ruff: noqa: E501
"""T17 model restriction/discovery QA harness.

Default offline contract:
  pytest tests/unit/test_litellm_model_restrictions.py -q

Opt-in live post-deploy verification:
  LITELLM_QA_ENABLE_LIVE=1 \
  LITELLM_QA_BASE_URL=https://litellm.example.com \
  LITELLM_QA_KEY_MY_FARM_ADVISOR=sk-... \
  LITELLM_QA_KEY_OPENCLAW=sk-... \
  LITELLM_QA_KEY_CODER_HERMES=sk-... \
  LITELLM_QA_KEY_CODER_KDENSE=sk-... \
  pytest tests/unit/test_litellm_model_restrictions.py -q -k live

The live path intentionally avoids `/health` because LiteLLM docs note that `/health`
can make upstream model calls. Discovery checks use `/v1/models` and `/model/info`, and
the completion probe uses a forbidden explicit alias that should fail before any paid call.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib import error, request

import pytest

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.dokploy.shared_core import build_litellm_consumer_model_allowlists
from dokploy_wizard.litellm.admin import (
    LiteLLMGatewayManager,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)
from dokploy_wizard.litellm.config_renderer import build_litellm_config

EXPECTED_VISIBLE_MODELS: dict[str, tuple[str, ...]] = {
    "my-farm-advisor": (
        "local/unsloth-active",
        "openai/*",
        "openrouter/auto",
        "openrouter/openrouter/free",
    ),
    "openclaw": (
        "local/unsloth-active",
        "openai/*",
        "openrouter/auto",
        "openrouter/openrouter/free",
        "nvidia/moonshotai/kimi-k2.5",
    ),
    "coder-hermes": (
        "local/unsloth-active",
        "openai/*",
        "openrouter/auto",
        "openrouter/openrouter/free",
    ),
    "coder-kdense": ("openai/*",),
}

LIVE_KEY_ENV_BY_CONSUMER = {
    "my-farm-advisor": "LITELLM_QA_KEY_MY_FARM_ADVISOR",
    "openclaw": "LITELLM_QA_KEY_OPENCLAW",
    "coder-hermes": "LITELLM_QA_KEY_CODER_HERMES",
    "coder-kdense": "LITELLM_QA_KEY_CODER_KDENSE",
}


@dataclass(frozen=True)
class HarnessResponse:
    status_code: int
    json_body: object


class FakeLiteLLMAdminApi:
    def __init__(self) -> None:
        self._teams: dict[str, LiteLLMTeamRecord] = {}
        self._keys: dict[str, LiteLLMVirtualKeyRecord] = {}

    def readiness(self) -> dict[str, object]:
        return {"status": "connected", "db": "connected"}

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return tuple(self._teams.values())

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(team_id=f"team-{team_alias}", team_alias=team_alias, models=models)
        self._teams[team_alias] = team
        return team

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return tuple(self._keys.values())

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        del metadata
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
        )
        self._keys[key_alias] = record
        return record


class FakeLiteLLMRestrictionHarness:
    def __init__(
        self,
        *,
        config: Mapping[str, object],
        keys_by_consumer: Mapping[str, LiteLLMVirtualKeyRecord],
    ) -> None:
        model_entries = cast(list[dict[str, object]], config["model_list"])
        self._models_by_name = {
            cast(str, entry["model_name"]): entry for entry in model_entries if isinstance(entry, dict)
        }
        self._keys_by_value = {record.key: record for record in keys_by_consumer.values()}

    def v1_models(self, api_key: str) -> HarnessResponse:
        record = self._authenticate(api_key)
        return HarnessResponse(
            status_code=200,
            json_body={
                "object": "list",
                "data": [
                    {"id": model_name, "object": "model", "owned_by": "litellm"}
                    for model_name in record.models
                    if model_name in self._models_by_name
                ],
            },
        )

    def model_info(self, api_key: str) -> HarnessResponse:
        record = self._authenticate(api_key)
        return HarnessResponse(
            status_code=200,
            json_body={
                "data": [
                    {
                        "model_name": model_name,
                        "litellm_params": self._models_by_name[model_name]["litellm_params"],
                        "model_info": {"id": f"fake-{index}-{model_name}"},
                    }
                    for index, model_name in enumerate(record.models, start=1)
                    if model_name in self._models_by_name
                ]
            },
        )

    def chat_completion(self, api_key: str, *, model: str) -> HarnessResponse:
        record = self._authenticate(api_key)
        if model not in record.models:
            return HarnessResponse(
                status_code=403,
                json_body={
                    "error": {
                        "message": f"Model '{model}' is not allowed for key alias '{record.key_alias}'."
                    }
                },
            )
        return HarnessResponse(
            status_code=200,
            json_body={
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            },
        )

    def _authenticate(self, api_key: str) -> LiteLLMVirtualKeyRecord:
        record = self._keys_by_value.get(api_key)
        if record is None:
            raise AssertionError(f"unknown fake LiteLLM key: {api_key}")
        return record


class LiveLiteLLMRestrictionHarness:
    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def v1_models(self, api_key: str) -> HarnessResponse:
        return self._request_json("GET", "/v1/models", api_key=api_key)

    def model_info(self, api_key: str) -> HarnessResponse:
        response = self._request_json("GET", "/model/info", api_key=api_key)
        if response.status_code != 404:
            return response
        return self._request_json("GET", "/v1/model/info", api_key=api_key)

    def chat_completion(self, api_key: str, *, model: str) -> HarnessResponse:
        return self._request_json(
            "POST",
            "/v1/chat/completions",
            api_key=api_key,
            payload={"model": model, "messages": [{"role": "user", "content": "ping"}]},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        payload: Mapping[str, object] | None = None,
    ) -> HarnessResponse:
        data = None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            url=f"{self._base_url}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with request.urlopen(req, timeout=30) as response:  # noqa: S310
                return HarnessResponse(
                    status_code=response.getcode(),
                    json_body=json.loads(response.read().decode("utf-8")),
                )
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            parsed_body: object
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = {"error": {"message": body or exc.reason}}
            return HarnessResponse(status_code=exc.code, json_body=parsed_body)


def _qa_flat_env() -> dict[str, str]:
    return {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
        "LITELLM_LOCAL_MODEL": "unsloth/Qwen2.5-Coder-32B-Instruct",
        "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
        "LITELLM_OPENROUTER_MODELS": (
            "openrouter/auto=openrouter/openai/gpt-4.1-mini,"
            "openrouter/openrouter/free=openrouter/google/gemma-4-31b-it:free"
        ),
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
        "LITELLM_NVIDIA_MODELS": "nvidia/moonshotai/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
        "OPENCLAW_NVIDIA_API_KEY": "openclaw-nvidia-upstream-key",
        "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
    }


def _expected_consumer_keys() -> dict[str, str]:
    return {
        "my-farm-advisor": "sk-test-my-farm-advisor",
        "openclaw": "sk-test-openclaw",
        "coder-hermes": "sk-test-coder-hermes",
        "coder-kdense": "sk-test-coder-kdense",
    }


def _build_fake_harness() -> FakeLiteLLMRestrictionHarness:
    flat_env = _qa_flat_env()
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "openclaw"),
    )
    allowlists = build_litellm_consumer_model_allowlists(flat_env=flat_env, plan=plan)
    manager = LiteLLMGatewayManager(api=FakeLiteLLMAdminApi(), sleep_fn=lambda _: None)
    reconciled = manager.reconcile_virtual_keys(
        generated_keys=_expected_consumer_keys(),
        consumer_model_allowlists=allowlists,
    )
    config = build_litellm_config(
        flat_env,
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
            "nvidia_api_key_env": "OPENCLAW_NVIDIA_API_KEY",
        },
    )
    return FakeLiteLLMRestrictionHarness(config=config, keys_by_consumer=reconciled)


def _consumer_api_key(consumer: str) -> str:
    return _expected_consumer_keys()[consumer]


def _model_names_from_v1_models(response: HarnessResponse) -> tuple[str, ...]:
    data = response.json_body
    if not isinstance(data, dict):
        raise AssertionError(f"expected dict v1/models response, got: {data!r}")
    raw_models = data.get("data")
    if not isinstance(raw_models, list):
        raise AssertionError(f"expected list in v1/models response, got: {raw_models!r}")
    names: list[str] = []
    for entry in raw_models:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            names.append(cast(str, entry["id"]))
    return tuple(names)


def _model_names_from_model_info(response: HarnessResponse) -> tuple[str, ...]:
    payload = response.json_body
    entries: object = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        entries = payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("model_name"), str):
        entries = [payload]
    if not isinstance(entries, list):
        raise AssertionError(f"expected list-like model/info response, got: {payload!r}")
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("model_name"), str):
            names.append(cast(str, entry["model_name"]))
    return tuple(names)


def _live_enabled() -> bool:
    return os.environ.get("LITELLM_QA_ENABLE_LIVE") == "1"


def _live_harness() -> LiveLiteLLMRestrictionHarness:
    base_url = os.environ.get("LITELLM_QA_BASE_URL", "").strip()
    if not base_url:
        raise AssertionError("LITELLM_QA_BASE_URL is required when LITELLM_QA_ENABLE_LIVE=1")
    return LiveLiteLLMRestrictionHarness(base_url=base_url)


def _live_key(consumer: str) -> str:
    env_name = LIVE_KEY_ENV_BY_CONSUMER[consumer]
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        raise AssertionError(f"{env_name} is required when LITELLM_QA_ENABLE_LIVE=1")
    return api_key


@pytest.fixture
def fake_harness() -> FakeLiteLLMRestrictionHarness:
    return _build_fake_harness()


def test_fake_v1_models_farm_key_shows_local_and_opencode_go_wildcard(
    fake_harness: FakeLiteLLMRestrictionHarness,
) -> None:
    response = fake_harness.v1_models(_consumer_api_key("my-farm-advisor"))

    assert response.status_code == 200
    assert _model_names_from_v1_models(response)[:2] == (
        "local/unsloth-active",
        "openai/*",
    )
    assert "openrouter/*" not in _model_names_from_v1_models(response)


@pytest.mark.parametrize(
    ("consumer", "expected_models"),
    tuple(EXPECTED_VISIBLE_MODELS.items()),
)
def test_fake_v1_models_expose_expected_aliases_only(
    fake_harness: FakeLiteLLMRestrictionHarness,
    consumer: str,
    expected_models: tuple[str, ...],
) -> None:
    response = fake_harness.v1_models(_consumer_api_key(consumer))

    assert response.status_code == 200
    assert _model_names_from_v1_models(response) == expected_models


@pytest.mark.parametrize(
    ("consumer", "expected_models"),
    tuple(EXPECTED_VISIBLE_MODELS.items()),
)
def test_fake_model_info_matches_expected_aliases_only(
    fake_harness: FakeLiteLLMRestrictionHarness,
    consumer: str,
    expected_models: tuple[str, ...],
) -> None:
    response = fake_harness.model_info(_consumer_api_key(consumer))

    assert response.status_code == 200
    assert _model_names_from_model_info(response) == expected_models


def test_fake_openrouter_wildcard_is_absent_for_all_restricted_keys(
    fake_harness: FakeLiteLLMRestrictionHarness,
) -> None:
    for consumer in EXPECTED_VISIBLE_MODELS:
        response = fake_harness.v1_models(_consumer_api_key(consumer))
        assert response.status_code == 200
        assert "openrouter/*" not in _model_names_from_v1_models(response)


def test_fake_opencode_go_wildcard_is_isolated_to_intended_alias_policy(
    fake_harness: FakeLiteLLMRestrictionHarness,
) -> None:
    visible_wildcards = {
        consumer: tuple(model for model in _model_names_from_v1_models(fake_harness.v1_models(_consumer_api_key(consumer))) if "*" in model)
        for consumer in EXPECTED_VISIBLE_MODELS
    }

    assert visible_wildcards == {
        "my-farm-advisor": ("openai/*",),
        "openclaw": ("openai/*",),
        "coder-hermes": ("openai/*",),
        "coder-kdense": ("openai/*",),
    }


@pytest.mark.parametrize("consumer", tuple(EXPECTED_VISIBLE_MODELS))
def test_fake_denied_unknown_openrouter_alias_returns_403(
    fake_harness: FakeLiteLLMRestrictionHarness,
    consumer: str,
) -> None:
    response = fake_harness.chat_completion(
        _consumer_api_key(consumer),
        model="openrouter/forbidden-model",
    )

    assert response.status_code in {400, 403}
    assert "forbidden-model" in json.dumps(response.json_body)


def test_fake_coder_kdense_rejects_advisor_only_openrouter_alias(
    fake_harness: FakeLiteLLMRestrictionHarness,
) -> None:
    response = fake_harness.chat_completion(
        _consumer_api_key("coder-kdense"),
        model="openrouter/openrouter/free",
    )

    assert response.status_code in {400, 403}


@pytest.mark.skipif(not _live_enabled(), reason="set LITELLM_QA_ENABLE_LIVE=1 to run post-deploy checks")
def test_live_litellm_restricted_keys_match_contract() -> None:
    harness = _live_harness()

    for consumer, expected_models in EXPECTED_VISIBLE_MODELS.items():
        v1_models = harness.v1_models(_live_key(consumer))
        model_info = harness.model_info(_live_key(consumer))
        denied_completion = harness.chat_completion(
            _live_key(consumer),
            model="openrouter/forbidden-model",
        )

        assert v1_models.status_code == 200
        assert _model_names_from_v1_models(v1_models) == expected_models
        assert "openrouter/*" not in _model_names_from_v1_models(v1_models)

        assert model_info.status_code == 200
        assert _model_names_from_model_info(model_info) == expected_models

        assert denied_completion.status_code in {400, 403}
