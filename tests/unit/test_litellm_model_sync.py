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
    OPENCODE_GO_GATEWAY_EXTRA_HEADERS,
    build_model_sync_sources,
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
    assert openrouter["model_ref_template"] == "openrouter/{model}"
    opencode_go = sources[1]
    assert opencode_go["list_url"] == "https://opencode.ai/zen/go/v1/models"
    assert opencode_go["api_base"] == "https://opencode.ai/zen/go/v1"
    assert opencode_go["model_ref_template"] == "openai/{model}"
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
