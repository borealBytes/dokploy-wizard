# mypy: ignore-errors
# ruff: noqa: E501
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from dokploy_wizard.litellm.model_sync import (
    MODEL_SYNC_CONSUMER_WILDCARD,
    MODEL_SYNC_DEFAULT_CHAT_ALIAS,
    MODEL_SYNC_DEFAULT_CHAT_PATTERN,
    MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS,
    MODEL_SYNC_NVIDIA_MAX_MODELS_DEFAULT,
    MODEL_SYNC_OPENCODE_GO_MAX_MODELS,
    MODEL_SYNC_OPENROUTER_MAX_MODELS,
    MODEL_SYNC_OPENROUTER_MAX_RANKED_FREE,
    MODEL_SYNC_OPENROUTER_MAX_RANKED_PAID,
    OPENCODE_GO_GATEWAY_EXTRA_HEADERS,
    build_default_chat_config,
    build_model_sync_sources,
    model_sync_default_chat_alias,
    model_sync_default_chat_pattern,
    model_sync_enabled,
    model_sync_interval_minutes,
    model_sync_max_models_per_provider,
    model_sync_script_text,
    model_sync_service_name,
    render_model_sync_sources_document,
)

_SIDECAR_PATH = Path("templates/litellm/model-sync/sync.py")


def _load_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("MODEL_SYNC_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setenv("MODEL_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key")
    monkeypatch.setenv("MODEL_SYNC_INTERVAL_MINUTES", "360")
    spec = importlib.util.spec_from_file_location("model_sync_sidecar", _SIDECAR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- default-chat: the pattern side ---------------------------------------------------

_GO_CREDS = {"opencode_go_api_key_env": "LITELLM_OPENCODE_GO_API_KEY"}
_OR_CREDS = {"openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY"}
_BOTH_CREDS = {**_GO_CREDS, **_OR_CREDS}
_GO_ENV = {"LITELLM_OPENCODE_GO_API_KEY": "go-key"}
_BOTH_ENV = {"LITELLM_OPENCODE_GO_API_KEY": "go-key", "LITELLM_OPENROUTER_API_KEY": "or-key"}


def test_default_chat_pattern_is_shipped_default_or_explicit_or_off() -> None:
    # Unset takes the shipped pattern, so a stack that never heard of this feature
    # still gets a default model that tracks the catalog instead of a frozen name.
    assert model_sync_default_chat_pattern({}) == MODEL_SYNC_DEFAULT_CHAT_PATTERN
    assert model_sync_default_chat_pattern({"AI_DEFAULT_MODEL_PATTERN": "glm*air*"}) == "glm*air*"
    # An explicit empty value is the operator turning it off.
    assert model_sync_default_chat_pattern({"AI_DEFAULT_MODEL_PATTERN": ""}) is None
    assert model_sync_default_chat_pattern({"AI_DEFAULT_MODEL_PATTERN": "   "}) is None
    # "*" names nothing: it matches the whole catalog, so it is treated as off rather
    # than silently making some arbitrary first row the stack's default model.
    assert model_sync_default_chat_pattern({"AI_DEFAULT_MODEL_PATTERN": "*"}) is None
    assert model_sync_default_chat_pattern({"AI_DEFAULT_MODEL_PATTERN": "**"}) is None


def test_default_chat_targets_only_the_provider_the_operator_chose() -> None:
    sources = build_model_sync_sources(_BOTH_ENV, _BOTH_CREDS)

    # AI_DEFAULT_PROVIDER is unset in this env, so the shipped provider (opencode-go)
    # is the one that carries the selection block.
    by_name = {str(source["name"]): source for source in sources}
    assert by_name["opencode-go"]["default_chat"] == {
        "pattern": MODEL_SYNC_DEFAULT_CHAT_PATTERN,
        "alias": MODEL_SYNC_DEFAULT_CHAT_ALIAS,
    }
    assert "default_chat" not in by_name["openrouter"]

    openrouter = build_model_sync_sources(
        {**_BOTH_ENV, "AI_DEFAULT_PROVIDER": "openrouter"}, _BOTH_CREDS
    )
    by_name = {str(source["name"]): source for source in openrouter}
    assert by_name["openrouter"]["default_chat"]["alias"] == MODEL_SYNC_DEFAULT_CHAT_ALIAS
    assert "default_chat" not in by_name["opencode-go"]

    # A provider with no sync source (a local endpoint) gets no selection at all:
    # there is no catalog to pick from, and inventing an alias nobody can route
    # through is worse than none.
    assert build_default_chat_config({"AI_DEFAULT_PROVIDER": "local"}) is None
    local_sources = build_model_sync_sources(
        {**_GO_ENV, "AI_DEFAULT_PROVIDER": "local", "LITELLM_MODEL_SYNC_PROVIDERS": "opencode-go"},
        _GO_CREDS,
    )
    assert all("default_chat" not in source for source in local_sources)


def test_default_chat_alias_needs_its_provider_to_actually_be_synced() -> None:
    env = {**_BOTH_ENV, "AI_DEFAULT_PROVIDER": "opencode-go"}
    assert model_sync_default_chat_alias(env, _BOTH_CREDS) == MODEL_SYNC_DEFAULT_CHAT_ALIAS

    # The alias is only worth pre-authorizing when its provider is being synced:
    # listing a name that can never exist on the gateway just adds a phantom row.
    assert (
        model_sync_default_chat_alias(
            {**env, "LITELLM_MODEL_SYNC_PROVIDERS": "openrouter"}, _BOTH_CREDS
        )
        is None
    )
    assert (
        model_sync_default_chat_alias({**env, "LITELLM_MODEL_SYNC_ENABLED": "false"}, _BOTH_CREDS)
        is None
    )
    # The operator named opencode-go but only has an OpenRouter key, so the go source
    # is never built and there is no catalog to select from: no alias to authorize.
    assert model_sync_default_chat_alias(env, _OR_CREDS) is None


def test_sources_cover_configured_providers_only() -> None:
    env = {
        "LITELLM_OPENROUTER_API_KEY": "or-key",
        "LITELLM_OPENCODE_GO_API_KEY": "go-key",
    }
    creds = {
        "openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY",
        "opencode_go_api_key_env": "LITELLM_OPENCODE_GO_API_KEY",
    }

    sources = build_model_sync_sources(env, creds)

    assert [source["name"] for source in sources] == ["openrouter", "opencode-go"]
    openrouter = sources[0]
    assert openrouter["alias_prefix"] == "sync/openrouter"
    assert openrouter["list_url"] == "https://openrouter.ai/api/v1/models"
    assert openrouter["rankings_url"] == "https://openrouter.ai/api/frontend/v1/rankings/models"
    assert openrouter["max_models"] == MODEL_SYNC_OPENROUTER_MAX_MODELS
    assert openrouter["max_ranked_paid"] == MODEL_SYNC_OPENROUTER_MAX_RANKED_PAID
    assert openrouter["max_ranked_free"] == MODEL_SYNC_OPENROUTER_MAX_RANKED_FREE
    assert openrouter["model_ref_template"] == "openrouter/{model}"
    opencode_go = sources[1]
    assert opencode_go["list_url"] == "https://opencode.ai/zen/go/v1/models"
    assert opencode_go["api_base"] == "https://opencode.ai/zen/go/v1"
    assert opencode_go["model_ref_template"] == "openai/{model}"
    assert opencode_go["max_models"] == MODEL_SYNC_OPENCODE_GO_MAX_MODELS
    assert opencode_go["extra_headers"] == dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS)
    assert model_sync_enabled(env, creds) is True


def test_sources_empty_without_any_upstream_key() -> None:
    assert build_model_sync_sources({}, {}) == ()
    assert model_sync_enabled({}, {}) is False


def test_sync_disabled_override_beats_configured_providers() -> None:
    env = {"LITELLM_MODEL_SYNC_ENABLED": "false"}
    creds = {"openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY"}

    assert model_sync_enabled(env, creds) is False


def test_provider_selection_and_interval_defaults() -> None:
    assert model_sync_interval_minutes({}) == 360
    assert model_sync_interval_minutes({"LITELLM_MODEL_SYNC_INTERVAL_MINUTES": "10"}) == 10
    assert model_sync_interval_minutes({"LITELLM_MODEL_SYNC_INTERVAL_MINUTES": "1"}) == 5
    assert model_sync_interval_minutes({"LITELLM_MODEL_SYNC_INTERVAL_MINUTES": "bad"}) == 360
    assert model_sync_max_models_per_provider({}) == 50
    assert model_sync_max_models_per_provider({"LITELLM_MODEL_SYNC_MAX_MODELS": "5"}) == 5


def test_sources_document_lists_selected_providers_and_wildcard() -> None:
    document = json.loads(
        render_model_sync_sources_document(
            {"LITELLM_OPENROUTER_API_KEY": "or-key", "LITELLM_MODEL_SYNC_PROVIDERS": "openrouter"},
            {"openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY"},
        )
    )

    assert document["interval_minutes"] == 360
    assert [source["name"] for source in document["sources"]] == ["openrouter"]
    assert MODEL_SYNC_CONSUMER_WILDCARD == "sync/*"
    assert model_sync_service_name("stack-shared-litellm") == "stack-shared-litellm-sync"


def test_sidecar_script_is_valid_python() -> None:
    compile(model_sync_script_text(), str(_SIDECAR_PATH), "exec")


def test_shared_core_plan_includes_model_sync_when_provider_key_configured() -> None:
    from dokploy_wizard.core.planner import build_shared_core_plan

    plan = build_shared_core_plan(
        stack_name="openmerge-me",
        enabled_packs=(),
        values={"LITELLM_OPENROUTER_API_KEY": "or-key"},
    )
    assert plan.model_sync is not None
    assert plan.model_sync.service_name == "openmerge-me-shared-litellm-sync"


def test_shared_core_plan_omits_model_sync_without_keys_or_when_disabled() -> None:
    from dokploy_wizard.core.planner import build_shared_core_plan

    without_keys = build_shared_core_plan(stack_name="openmerge-me", enabled_packs=(), values={})
    assert without_keys.model_sync is None

    disabled = build_shared_core_plan(
        stack_name="openmerge-me",
        enabled_packs=(),
        values={
            "LITELLM_OPENROUTER_API_KEY": "or-key",
            "LITELLM_MODEL_SYNC_ENABLED": "false",
        },
    )
    assert disabled.model_sync is None


def test_readme_documents_model_autosync() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "### Model autosync" in readme
    assert "sync/<source>/<model-id>" in readme
    assert "LITELLM_MODEL_SYNC_ENABLED" in readme
    assert "LITELLM_MODEL_SYNC_INTERVAL_MINUTES" in readme
    assert "store_model_in_db" in readme


def _sidecar_document() -> dict:
    return {
        "interval_minutes": 360,
        "max_models_per_provider": 2,
        "sources": [
            {
                "name": "openrouter",
                "alias_prefix": "sync/openrouter",
                "list_url": "https://openrouter.ai/api/v1/models",
                "api_key_env": "LITELLM_OPENROUTER_API_KEY",
                "model_ref_template": "openrouter/{model}",
            }
        ],
    }


def _sidecar_transport(document: dict, gateway_models: dict):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        calls.append((method, url, payload))
        if url.endswith("/api/v1/models"):
            assert token == "or-upstream-key"
            return {"data": [{"id": "deepseek/new-thing"}, {"id": "wild/*"}]}
        if url.endswith("/model/info"):
            assert token == "master-key"
            return {"data": gateway_models}
        return {}

    return fake_http_json, calls


def test_sidecar_creates_new_models_and_prunes_gone_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_OPENROUTER_API_KEY", "or-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    gateway_models = [
        {
            "model_name": "sync/openrouter/old/gone-model",
            "model_info": {
                "id": "db-id-gone",
                "metadata": {"managed_by": "dokploy-wizard-model-sync"},
            },
        },
        {
            "model_name": "sync/openrouter/operator-kept",
            "model_info": {"id": "db-id-kept", "metadata": {}},
        },
        {
            "model_name": "openrouter/static-alias",
            "model_info": {"id": "db-id-static", "metadata": {}},
        },
    ]
    sidecar.http_json, calls = _sidecar_transport(_sidecar_document(), gateway_models)

    summary = sidecar.sync_once(_sidecar_document())

    assert summary["created"] == 1
    assert summary["pruned"] == 1
    create = next(call for call in calls if call[1].endswith("/model/new"))
    assert create[2]["model_name"] == "sync/openrouter/deepseek/new-thing"
    assert create[2]["litellm_params"] == {
        "model": "openrouter/deepseek/new-thing",
        "api_key": "or-upstream-key",
    }
    assert create[2]["model_info"]["metadata"]["managed_by"] == "dokploy-wizard-model-sync"
    delete = next(call for call in calls if call[1].endswith("/model/delete"))
    assert delete[2] == {"id": "db-id-gone"}
    assert all(call[2] != {"id": "db-id-kept"} and call[2] != {"id": "db-id-static"} for call in calls if call[1].endswith("/model/delete"))


def test_sidecar_applies_source_extra_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_OPENCODE_GO_API_KEY", "zen-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = {
        "interval_minutes": 360,
        "max_models_per_provider": 2,
        "sources": [
            {
                "name": "opencode-go",
                "alias_prefix": "sync/opencode-go",
                "list_url": "https://opencode.ai/zen/go/v1/models",
                "api_key_env": "LITELLM_OPENCODE_GO_API_KEY",
                "api_base": "https://opencode.ai/zen/go/v1",
                "model_ref_template": "openai/{model}",
                "extra_headers": dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS),
            }
        ],
    }
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        calls.append((method, url, payload))
        if url.endswith("/v1/models"):
            assert token == "zen-upstream-key"
            return {"data": [{"id": "deepseek-v4-flash"}]}
        if url.endswith("/model/info"):
            return {"data": []}
        return {}

    sidecar.http_json = fake_http_json

    summary = sidecar.sync_once(document)

    assert summary["created"] == 1
    create = next(call for call in calls if call[1].endswith("/model/new"))
    assert create[2]["model_name"] == "sync/opencode-go/deepseek-v4-flash"
    assert create[2]["litellm_params"] == {
        "model": "openai/deepseek-v4-flash",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "zen-upstream-key",
        "extra_headers": dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS),
    }


def test_sidecar_keeps_models_when_source_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_OPENROUTER_API_KEY", "or-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)

    def failing_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        if "openrouter.ai" in url:
            raise RuntimeError("upstream exploded")
        return {
            "data": [
                {
                    "model_name": "sync/openrouter/deepseek/new-thing",
                    "model_info": {
                        "id": "db-id-1",
                        "metadata": {"managed_by": "dokploy-wizard-model-sync"},
                    },
                }
            ]
        }

    sidecar.http_json = failing_http_json

    summary = sidecar.sync_once(_sidecar_document())

    assert summary["created"] == 0
    assert summary["pruned"] == 0
    assert summary["sources_ok"] == []


_RANKED_CATALOG = {
    "data": [
        {
            "id": "deepseek/deepseek-v4-flash",
            "canonical_slug": "deepseek/deepseek-v4-flash-20260423",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "deepseek/deepseek-v4-flash:free",
            "canonical_slug": "deepseek/deepseek-v4-flash-20260423",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "z-ai/glm-5.3",
            "canonical_slug": "z-ai/glm-5.3-20260816",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "openai/gpt-6-luna:batch",
            "canonical_slug": "openai/gpt-6-luna-20260922",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "anthropic/visionary-pro",
            "canonical_slug": "anthropic/visionary-pro-20260101",
            "architecture": {"output_modalities": ["image"]},
        },
        {
            "id": "xiaomi/mimo-v2.6-flash:free",
            "canonical_slug": "xiaomi/mimo-v2.6-flash-20260921",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "tencent/hy4-preview",
            "canonical_slug": "tencent/hy4-preview-20260827",
            "architecture": {"output_modalities": ["text"]},
        },
    ]
}


def _ranked_source(**overrides: object) -> dict:
    source: dict = {
        "name": "openrouter",
        "alias_prefix": "sync/openrouter",
        "list_url": "https://openrouter.ai/api/v1/models",
        "rankings_url": "https://openrouter.ai/api/frontend/v1/rankings/models",
        "api_key_env": "LITELLM_OPENROUTER_API_KEY",
        "model_ref_template": "openrouter/{model}",
        "max_models": 30,
        "max_ranked_paid": 3,
        "max_ranked_free": 2,
    }
    source.update(overrides)
    return source


def _ranked_rows() -> dict:
    return {
        "data": [
            {"model_permaslug": "openai/gpt-6-luna-20260922", "variant": "standard", "rankingMetricValue": 35},
            {"model_permaslug": "deepseek/deepseek-v4-flash-20260423", "variant": "standard", "rankingMetricValue": 25},
            {"model_permaslug": "deepseek/deepseek-v4-flash-20260423", "variant": "standard", "rankingMetricValue": 15},
            {"model_permaslug": "anthropic/visionary-pro-20260101", "variant": "standard", "rankingMetricValue": 33},
            {"model_permaslug": "z-ai/glm-5.3-20260816", "variant": "standard", "rankingMetricValue": 30},
            {"model_permaslug": "tencent/hy4-preview-20260827", "variant": "standard", "rankingMetricValue": 10},
            {"model_permaslug": "xiaomi/mimo-v2.6-flash-20260921", "variant": "free", "rankingMetricValue": 20},
            {"model_permaslug": "deepseek/deepseek-v4-flash-20260423", "variant": "free", "rankingMetricValue": 5},
        ]
    }


def _ranked_transport(monkeypatch: pytest.MonkeyPatch, rankings: dict | Exception):
    monkeypatch.setenv("LITELLM_OPENROUTER_API_KEY", "or-upstream-key")

    calls: list[tuple[str, str, str | None]] = []

    def fake_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        calls.append((method, url, token))
        if url.endswith("/api/frontend/v1/rankings/models"):
            if isinstance(rankings, Exception):
                raise rankings
            return rankings
        return _RANKED_CATALOG

    return fake_http_json, calls


def test_sidecar_ranks_openrouter_models_paid_then_free_by_leaderboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    sidecar.http_json, calls = _ranked_transport(monkeypatch, _ranked_rows())

    model_ids = sidecar.source_model_ids(_ranked_source(), 50)

    assert model_ids == [
        "deepseek/deepseek-v4-flash",
        "z-ai/glm-5.3",
        "tencent/hy4-preview",
        "xiaomi/mimo-v2.6-flash:free",
        "deepseek/deepseek-v4-flash:free",
    ]
    rankings_call = next(call for call in calls if call[1].endswith("/rankings/models"))
    assert rankings_call[2] is None


def test_sidecar_ranked_total_cap_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    sidecar.http_json, _calls = _ranked_transport(monkeypatch, _ranked_rows())
    assert sidecar.source_model_ids(_ranked_source(max_models=2), 50) == [
        "deepseek/deepseek-v4-flash",
        "z-ai/glm-5.3",
    ]

    sidecar.http_json, _calls2 = _ranked_transport(
        monkeypatch, RuntimeError("rankings offline")
    )
    fallback = sidecar.source_model_ids(_ranked_source(max_models=3), 50)
    assert fallback == [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash:free",
        "z-ai/glm-5.3",
    ]


def test_sidecar_source_budget_overrides_global_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)

    def fake_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        return {"data": [{"id": f"model-{index}"} for index in range(5)]}

    sidecar.http_json = fake_http_json
    source = {
        "name": "opencode-go",
        "alias_prefix": "sync/opencode-go",
        "list_url": "https://opencode.ai/zen/go/v1/models",
        "model_ref_template": "openai/{model}",
        "max_models": 100,
    }
    assert len(sidecar.source_model_ids(source, 2)) == 5


def test_sidecar_refreshes_consumer_key_allowlists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_OPENROUTER_API_KEY", "or-upstream-key")
    keys_path = tmp_path / "state" / "consumer-keys.json"
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MODEL_SYNC_CONSUMER_KEYS_FILE", str(keys_path))
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    keys_path.write_text(json.dumps({"coder-hermes": "sk-consumer-1"}), encoding="utf-8")

    calls: list[tuple[str, str, dict | None]] = []

    current_models = [
        "opencode-go/deepseek-v4-flash",
        "sync/*",
        "sync/openrouter/stale/gone-model",
    ]

    def fake_http_json(method: str, url: str, *, token: str | None = None, payload: dict | None = None):
        calls.append((method, url, payload))
        if url.endswith("/api/v1/models"):
            return {"data": [{"id": "deepseek/new-thing"}, {"id": "wild/*"}]}
        if url.endswith("/model/info"):
            return {"data": []}
        if url.startswith("http://litellm:4000/key/info"):
            assert url.endswith("key=sk-consumer-1")
            return {"info": {"models": list(current_models)}}
        if url.endswith("/key/update"):
            current_models[:] = payload["models"]
        return {}

    sidecar.http_json = fake_http_json

    summary = sidecar.sync_once(_sidecar_document())

    assert summary["key_reconciled"] == 1
    assert summary["key_errors"] == 0
    update = next(call for call in calls if call[1].endswith("/key/update"))
    # The stale sync alias is dropped, the wildcard and static route survive,
    # and the freshly created alias is appended.
    assert update[2]["models"] == [
        "opencode-go/deepseek-v4-flash",
        "sync/*",
        "sync/openrouter/deepseek/new-thing",
    ]

    calls.clear()
    summary_again = sidecar.sync_once(_sidecar_document())
    assert summary_again["key_reconciled"] == 0


def test_sidecar_skips_key_reconcile_without_consumer_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_OPENROUTER_API_KEY", "or-upstream-key")
    monkeypatch.setenv("MODEL_SYNC_CONSUMER_KEYS_FILE", str(tmp_path / "absent.json"))
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    sidecar.http_json, calls = _sidecar_transport(_sidecar_document(), [])

    summary = sidecar.sync_once(_sidecar_document())

    assert summary["key_reconciled"] == 0
    assert summary["key_errors"] == 0
    assert not [call for call in calls if call[1].endswith("/key/update")]


def test_sidecar_healthcheck_requires_fresh_success_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)

    assert sidecar.run_healthcheck() == 1

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last_success").write_text(json.dumps({"ts": int(__import__("time").time())}), encoding="utf-8")
    assert sidecar.run_healthcheck() == 0

    (state_dir / "last_success").write_text(json.dumps({"ts": 0}), encoding="utf-8")
    assert sidecar.run_healthcheck() == 1


# --- NVIDIA liveness probe + top-N cap -------------------------------------

_NVIDIA_CATALOG = [
    "meta/live-one",
    "bigcode/dead-one",
    "deepseek/live-two",
    "nvidia/dead-two",
    "mistral/weird",
    "qwen/live-three",
    "ibm/live-four",
]


def _nvidia_document(max_models: int = 3, probe: object = True) -> dict:
    source: dict = {
        "name": "nvidia",
        "alias_prefix": "sync/nvidia",
        "list_url": "https://integrate.api.nvidia.com/v1/models",
        "api_key_env": "LITELLM_NVIDIA_API_KEY",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "model_ref_template": "openai/{model}",
        "max_models": max_models,
    }
    if probe is not None:
        source["probe"] = probe
        source["probe_ttl_days"] = 7
    return {"interval_minutes": 360, "max_models_per_provider": 50, "sources": [source]}


def _nvidia_transport(probe_calls: list, *, dead: set, errors: set, catalog=None):
    from urllib.error import HTTPError

    def fake_http_json(method, url, *, token=None, payload=None, timeout=30):
        if url.endswith("/v1/models"):
            return {"data": [{"id": model} for model in (catalog or _NVIDIA_CATALOG)]}
        if url.endswith("/model/info"):
            return {"data": []}
        if url.endswith("/chat/completions"):
            model = payload["model"]
            probe_calls.append(model)
            assert payload["max_tokens"] == 1
            if model in dead:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if model in errors:
                raise RuntimeError("connection reset")
            return {"choices": [{"message": {"content": "ok"}}]}
        return {}

    return fake_http_json


def _created_aliases(calls) -> set:
    return {
        call[2]["model_name"]
        for call in calls
        if call[1].endswith("/model/new")
    }


def _capture_creates(sidecar, base_fake):
    calls: list = []

    def fake_http_json(method, url, *, token=None, payload=None, timeout=30):
        calls.append((method, url, payload))
        return base_fake(method, url, token=token, payload=payload, timeout=timeout)

    sidecar.http_json = fake_http_json
    return calls


def test_nvidia_source_declares_probe_and_cap() -> None:
    env = {"LITELLM_NVIDIA_API_KEY": "nv-key"}
    creds = {"nvidia_api_key_env": "LITELLM_NVIDIA_API_KEY"}

    nvidia = build_model_sync_sources(env, creds)[0]
    assert nvidia["probe"] is True
    assert nvidia["max_models"] == MODEL_SYNC_NVIDIA_MAX_MODELS_DEFAULT
    assert nvidia["probe_ttl_days"] == MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS
    assert nvidia["free_only"] is True
    assert nvidia["free_catalog_url"] == "https://models.dev/api.json"
    assert nvidia["free_catalog_provider"] == "nvidia"
    assert nvidia["alias_suffix"] == ":free"

    overridden = build_model_sync_sources(
        {
            "LITELLM_NVIDIA_API_KEY": "nv-key",
            "LITELLM_MODEL_SYNC_NVIDIA_PROBE": "0",
            "LITELLM_MODEL_SYNC_NVIDIA_MAX_MODELS": "5",
            "LITELLM_MODEL_SYNC_NVIDIA_FREE_ONLY": "0",
            "LITELLM_MODEL_SYNC_PROBE_TTL_DAYS": "2",
        },
        creds,
    )[0]
    assert overridden["probe"] is False
    assert overridden["max_models"] == 5
    assert overridden["free_only"] is False
    assert overridden["probe_ttl_days"] == 2


def test_sidecar_probe_drops_dead_models_and_caps_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes: list = []
    calls = _capture_creates(
        sidecar,
        _nvidia_transport(probes, dead={"bigcode/dead-one", "nvidia/dead-two"}, errors={"mistral/weird"}),
    )

    summary = sidecar.sync_once(_nvidia_document(max_models=3))

    assert set(probes) == set(_NVIDIA_CATALOG)
    # Dead models never sync; inconclusive (non-404 failures) are kept; the
    # budget then applies to the surviving set in catalog order.
    assert _created_aliases(calls) == {
        "sync/nvidia/meta/live-one",
        "sync/nvidia/deepseek/live-two",
        "sync/nvidia/mistral/weird",
    }
    assert summary["created"] == 3


def test_sidecar_probe_cache_skips_known_alive_reprobes_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes_first: list = []
    sidecar.http_json = _nvidia_transport(
        probes_first, dead={"bigcode/dead-one", "nvidia/dead-two"}, errors={"mistral/weird"}
    )
    sidecar.sync_once(_nvidia_document(max_models=3))
    assert set(probes_first) == set(_NVIDIA_CATALOG)

    probes_second: list = []
    sidecar.http_json = _nvidia_transport(
        probes_second, dead={"bigcode/dead-one"}, errors={"mistral/weird"}
    )
    calls = _capture_creates(
        sidecar,
        _nvidia_transport(probes_second, dead={"bigcode/dead-one"}, errors={"mistral/weird"}),
    )
    sidecar.sync_once(_nvidia_document(max_models=4))

    # Known-alive models ride the cache; dead and inconclusive ones re-probe
    # every cycle so recovered models return without waiting for TTL expiry.
    assert set(probes_second) == {"bigcode/dead-one", "nvidia/dead-two", "mistral/weird"}
    assert "sync/nvidia/nvidia/dead-two" in _created_aliases(calls)


def test_sidecar_probe_cache_expiry_forces_full_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    sidecar.http_json = _nvidia_transport([], dead=set(), errors=set())
    sidecar.sync_once(_nvidia_document(max_models=3))

    cache_path = tmp_path / "state" / "probe-cache-nvidia.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["checked_at"] -= 8 * 86400  # beyond the 7-day TTL
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    probes: list = []
    sidecar.http_json = _nvidia_transport(probes, dead=set(), errors=set())
    sidecar.sync_once(_nvidia_document(max_models=3))
    assert set(probes) == set(_NVIDIA_CATALOG)


def test_sidecar_probe_brownout_keeps_full_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes: list = []
    # Upstream brownout: everything 404s, including models that work fine.
    calls = _capture_creates(sidecar, _nvidia_transport(probes, dead=set(_NVIDIA_CATALOG), errors=set()))

    sidecar.sync_once(_nvidia_document(max_models=3))

    assert _created_aliases(calls) == {
        "sync/nvidia/meta/live-one",
        "sync/nvidia/bigcode/dead-one",
        "sync/nvidia/deepseek/live-two",
    }
    # A brownout must not poison the cache: everything is re-probed next cycle.
    probes_second: list = []
    sidecar.http_json = _nvidia_transport(probes_second, dead=set(), errors=set())
    sidecar.sync_once(_nvidia_document(max_models=3))
    assert set(probes_second) == set(_NVIDIA_CATALOG)


def test_sidecar_probe_disabled_skips_chat_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes: list = []

    def fake_http_json(method, url, *, token=None, payload=None, timeout=30):
        if url.endswith("/chat/completions"):
            probes.append(payload["model"])
            return {"choices": []}
        if url.endswith("/v1/models"):
            return {"data": [{"id": model} for model in _NVIDIA_CATALOG]}
        if url.endswith("/model/info"):
            return {"data": []}
        return {}

    calls = _capture_creates(sidecar, fake_http_json)

    sidecar.sync_once(_nvidia_document(max_models=3, probe=False))

    assert probes == []
    assert _created_aliases(calls) == {
        "sync/nvidia/meta/live-one",
        "sync/nvidia/bigcode/dead-one",
        "sync/nvidia/deepseek/live-two",
    }


_FREE_CATALOG = {
    "nvidia": {
        "models": {
            "meta/live-one": {"cost": {"input": 0, "output": 0}},
            "deepseek/live-two": {"cost": {"input": 0, "output": 0}},
            "meta/free-named": {"cost": {"input": 0, "output": 0}},
            "qwen/paid": {"cost": {"input": 0.002, "output": 0.006}},
            "ibm/paid": {"cost": {"input": 0.001, "output": 0.003}},
            "mistral/dead-free": {"cost": {"input": 0, "output": 0}},
        }
    }
}


def _free_nvidia_document(max_models: int = 5) -> dict:
    return {
        "interval_minutes": 360,
        "max_models_per_provider": 50,
        "sources": [
            {
                "name": "nvidia",
                "alias_prefix": "sync/nvidia",
                "list_url": "https://integrate.api.nvidia.com/v1/models",
                "api_key_env": "LITELLM_NVIDIA_API_KEY",
                "api_base": "https://integrate.api.nvidia.com/v1",
                "model_ref_template": "openai/{model}",
                "max_models": max_models,
                "probe": True,
                "probe_ttl_days": 7,
                "free_only": True,
                "free_catalog_url": "https://models.dev/api.json",
                "free_catalog_provider": "nvidia",
                "alias_suffix": ":free",
            }
        ],
    }


def _free_filter_transport(probe_calls: list, *, dead: set, registry_fail: bool = False):
    catalog = [
        "meta/live-one",
        "qwen/paid",
        "deepseek/live-two",
        "ibm/paid",
        "meta/free-named",
        "mistral/dead-free",
    ]

    def fake_http_json(method, url, *, token=None, payload=None, timeout=30):
        if url.endswith("/v1/models"):
            return {"data": [{"id": model} for model in catalog]}
        if url.endswith("/api.json"):
            if registry_fail:
                raise RuntimeError("registry unreachable")
            return _FREE_CATALOG
        if url.endswith("/model/info"):
            return {"data": []}
        if url.endswith("/chat/completions"):
            model = payload["model"]
            probe_calls.append(model)
            if model in dead:
                from urllib.error import HTTPError

                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"choices": [{"message": {"content": "ok"}}]}
        return {}

    return fake_http_json


def _capture_sidecar(sidecar, base_fake):
    calls: list = []

    def fake_http_json(method, url, *, token=None, payload=None, timeout=30):
        calls.append((method, url, payload))
        return base_fake(method, url, token=token, payload=payload, timeout=timeout)

    sidecar.http_json = fake_http_json
    return calls


def test_sidecar_free_only_filter_drops_paid_models_before_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes: list = []
    calls = _capture_sidecar(sidecar, _free_filter_transport(probes, dead={"mistral/dead-free"}))

    sidecar.sync_once(_free_nvidia_document(max_models=5))

    # Paid endpoints never reach the picker and are never chat-probed.
    assert set(probes) == {
        "meta/live-one",
        "deepseek/live-two",
        "meta/free-named",
        "mistral/dead-free",
    }
    # Free-verified aliases carry a ":free" tail unless the name already
    # says free, so pickers advertise the zero cost.
    assert _created_aliases(calls) == {
        "sync/nvidia/meta/live-one:free",
        "sync/nvidia/deepseek/live-two:free",
        "sync/nvidia/meta/free-named",
    }


def test_sidecar_free_registry_failure_keeps_full_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_NVIDIA_API_KEY", "nv-upstream-key")
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    probes: list = []
    calls = _capture_sidecar(
        sidecar, _free_filter_transport(probes, dead=set(), registry_fail=True)
    )

    sidecar.sync_once(_free_nvidia_document(max_models=5))

    # Registry unreachable -> fail open: nothing is silently dropped, the
    # liveness probe still gates the result, and no ":free" label is claimed
    # because free tier was never verified.
    assert set(probes) == {
        "meta/live-one",
        "qwen/paid",
        "deepseek/live-two",
        "ibm/paid",
        "meta/free-named",
        "mistral/dead-free",
    }
    assert _created_aliases(calls) == {
        "sync/nvidia/meta/live-one",
        "sync/nvidia/qwen/paid",
        "sync/nvidia/deepseek/live-two",
        "sync/nvidia/ibm/paid",
        "sync/nvidia/meta/free-named",
    }
def _default_chat_document(**overrides: object) -> dict:
    source = {
        "name": "opencode-go",
        "alias_prefix": "sync/opencode-go",
        "list_url": "https://opencode.ai/zen/go/v1/models",
        "api_key_env": "LITELLM_OPENCODE_GO_API_KEY",
        "api_base": "https://opencode.ai/zen/go/v1",
        "model_ref_template": "openai/{model}",
        "max_models": 100,
        "extra_headers": {"x-opencode-session": "gw", "User-Agent": "wizard/1"},
        "default_chat": {"pattern": "deepseek*flash*", "alias": MODEL_SYNC_DEFAULT_CHAT_ALIAS},
    }
    document = {"interval_minutes": 360, "max_models_per_provider": 50, "sources": [source]}
    document.update(overrides)
    return document


def _selection_transport(
    monkeypatch: pytest.MonkeyPatch,
    model_ids: list[str],
    *,
    alive: set[str] | None = None,
    dead: set[str] | None = None,
) -> tuple[object, list[dict]]:
    """Fake transport for the sidecar's catalog, probe, and admin calls."""

    alive = alive or set()
    dead = dead or set()
    calls: list[dict] = []

    def fake_http_json(
        method: str,
        url: str,
        *,
        token: object = None,
        payload: object = None,
        timeout: object = None,
        headers: object = None,
    ) -> dict:
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "timeout": timeout,
                "headers": headers,
            }
        )
        if url.endswith("/models"):
            return {"data": [{"id": model_id} for model_id in model_ids]}
        if url.endswith("/chat/completions"):
            model = str((payload or {}).get("model"))
            if model in dead:
                raise _HttpError(404)
            if model not in alive:
                raise _HttpError(429)
            return {"choices": [{"message": {"content": "pong"}}]}
        if url.endswith("/model/new"):
            return {"info": {}}
        if url.endswith("/model/delete"):
            return {"info": {}}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setenv("LITELLM_OPENCODE_GO_API_KEY", "go-key")
    return fake_http_json, calls


class _HttpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("deepseek-v4.10-flash", (4, 10)),
        ("deepseek-v4.1-flash", (4, 1)),
        ("deepseek-v4-flash", (4,)),
        ("deepseek-flash", (0,)),
    ],
)
def test_version_rank_orders_by_numbers_not_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_id: str, expected: tuple
) -> None:
    # "v4.10" sorts below "v4.2" as a string, which would hand the default alias to an
    # older model forever; the tuple comparison is what makes 4.10 outrank 4.2.
    sidecar = _load_sidecar(tmp_path, monkeypatch)

    assert sidecar.version_rank(model_id) == expected


def test_default_chat_candidates_rank_newest_first_with_catalog_order_on_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, _calls = _selection_transport(
        monkeypatch,
        [
            "deepseek-v4.1-flash",
            "deepseek-v4.10-flash",
            "deepseek-v4-flash",
            "glm-4.6-air",  # does not match the pattern
            "kimi-k2",
        ],
    )

    candidates = sidecar.default_chat_candidates(
        document["sources"][0], "go-key", "deepseek*flash*"
    )

    assert candidates == ["deepseek-v4.10-flash", "deepseek-v4.1-flash", "deepseek-v4-flash"]


def test_default_chat_promotes_the_confirmed_model_before_retiring_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, calls = _selection_transport(
        monkeypatch,
        ["deepseek-v4.10-flash", "deepseek-v4.1-flash"],
        # The newest model is rate limited (inconclusive) and the older one answers:
        # the alias must land on the model that actually completes a request.
        alive={"deepseek-v4.1-flash"},
    )
    existing = {
        MODEL_SYNC_DEFAULT_CHAT_ALIAS: {
            "id": "dep-old",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-old", "target": "openai/deepseek-v3-flash"}],
        }
    }

    result = sidecar.resolve_default_chat(document, existing)

    assert result["status"] == "confirmed"
    assert result["target"] == "openai/deepseek-v4.1-flash"
    assert result["changed"] is True
    assert result["retired"] == 1
    created = [c for c in calls if c["url"].endswith("/model/new")]
    deleted = [c for c in calls if c["url"].endswith("/model/delete")]
    assert len(created) == 1
    assert created[0]["payload"]["model_name"] == MODEL_SYNC_DEFAULT_CHAT_ALIAS
    assert created[0]["payload"]["litellm_params"]["model"] == "openai/deepseek-v4.1-flash"
    # Promote before retire: the new target is registered first so the alias never
    # points at nothing while the switch happens.
    assert calls.index(created[0]) < calls.index(deleted[0])
    assert deleted[0]["payload"] == {"id": "dep-old"}


def test_default_chat_probes_with_the_source_session_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenCode Go rejects requests without its session header, so a probe that omits
    # it reports every candidate as inconclusive and the alias never resolves.
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, calls = _selection_transport(
        monkeypatch, ["deepseek-v4.1-flash"], alive={"deepseek-v4.1-flash"}
    )

    result = sidecar.resolve_default_chat(document, {})

    assert result["status"] == "confirmed"
    probes = [c for c in calls if c["url"].endswith("/chat/completions")]
    assert probes
    assert probes[0]["headers"]["x-opencode-session"] == "gw"
    assert probes[0]["payload"]["max_tokens"] == sidecar.DEFAULT_CHAT_PROBE_MAX_TOKENS
    assert probes[0]["payload"]["max_tokens"] >= 16


def test_default_chat_keeps_the_current_target_when_nothing_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, calls = _selection_transport(
        monkeypatch, ["deepseek-v4.10-flash", "deepseek-v4.1-flash"], alive=set()
    )
    existing = {
        MODEL_SYNC_DEFAULT_CHAT_ALIAS: {
            "id": "dep-old",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-old", "target": "openai/deepseek-v3-flash"}],
        }
    }

    result = sidecar.resolve_default_chat(document, existing)

    assert result["status"] == "unconfirmed"
    # A brownout must not move the default off a target that was working, and it must
    # not delete the alias either: that would take the default away from every client.
    assert not [c for c in calls if c["url"].endswith("/model/delete")]
    assert not [c for c in calls if c["url"].endswith("/model/new")]


def test_default_chat_drops_a_model_the_provider_does_not_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, calls = _selection_transport(
        monkeypatch,
        ["deepseek-v4.10-flash", "deepseek-v4.1-flash"],
        dead={"deepseek-v4.10-flash"},
        alive={"deepseek-v4.1-flash"},
    )

    result = sidecar.resolve_default_chat(document, {})

    assert result["target"] == "openai/deepseek-v4.1-flash"
    probed = [c["payload"]["model"] for c in calls if c["url"].endswith("/chat/completions")]
    assert probed == ["deepseek-v4.10-flash", "deepseek-v4.1-flash"]


def test_default_chat_stays_put_when_the_alias_already_points_at_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    sidecar.http_json, calls = _selection_transport(
        monkeypatch, ["deepseek-v4.1-flash"], alive={"deepseek-v4.1-flash"}
    )
    existing = {
        MODEL_SYNC_DEFAULT_CHAT_ALIAS: {
            "id": "dep-1",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-1", "target": "openai/deepseek-v4.1-flash"}],
        }
    }

    result = sidecar.resolve_default_chat(document, existing)

    assert result == {
        "status": "confirmed",
        "source": "opencode-go",
        "alias": MODEL_SYNC_DEFAULT_CHAT_ALIAS,
        "target": "openai/deepseek-v4.1-flash",
        "changed": False,
    }
    assert not [c for c in calls if c["url"].endswith("/model/new")]
    assert not [c for c in calls if c["url"].endswith("/model/delete")]


def test_default_chat_is_never_touched_by_the_source_prune_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prune loop keys on sync/<source>/... and the default alias is one segment
    # deep, so the loop must leave it alone even when its provider is unhealthy; only
    # selection may change it.
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    pruned: list[object] = []
    sidecar.delete_deployment = lambda model_id: pruned.append(model_id)
    existing = {
        MODEL_SYNC_DEFAULT_CHAT_ALIAS: {
            "id": "dep-default",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-default", "target": "openai/deepseek-v4.1-flash"}],
        },
        "sync/openrouter/gone-model": {
            "id": "dep-or",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-or", "target": "openrouter/gone"}],
        },
    }

    # Source "openrouter" is healthy but stopped reporting the model; "opencode-go"
    # is down entirely. Neither may take the default alias with it.
    pruned_count, _ = sidecar._prune_deployments(
        existing, deployments={}, ok_sources={"openrouter"}
    )

    assert pruned == ["dep-or"]
    assert pruned_count == 1


def test_default_chat_alias_is_retired_when_the_feature_is_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Turning the feature off removes the default_chat block from every source. The
    # alias has to go with it, or consumers keep pointing at a model that nothing
    # selects any more, and the prune loop cannot do it because of the name depth.
    sidecar = _load_sidecar(tmp_path, monkeypatch)
    document = _default_chat_document()
    document["sources"][0].pop("default_chat")
    deleted: list[object] = []
    sidecar.delete_deployment = lambda model_id: deleted.append(model_id)
    existing = {
        MODEL_SYNC_DEFAULT_CHAT_ALIAS: {
            "id": "dep-default",
            "managed_by": sidecar.MANAGED_BY,
            "targets": [{"id": "dep-default", "target": "openai/deepseek-v4.1-flash"}],
        }
    }

    result = sidecar.resolve_default_chat(document, existing)

    assert result["status"] == "disabled"
    assert result["retired"] == 1
    assert deleted == ["dep-default"]


