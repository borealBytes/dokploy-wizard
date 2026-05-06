# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any, cast

from dokploy_wizard.litellm.config_renderer import build_litellm_config, render_litellm_config_yaml


def _model_list(config: dict[str, object]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], config["model_list"])


def test_build_litellm_config_keeps_only_local_route_when_non_local_routes_paused() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "AI_DEFAULT_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free="
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free,"
                "openrouter/healer-alpha=openrouter/anthropic/claude-3.5-sonnet"
            ),
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == ["local/unsloth-active", "unsloth-active"]
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[0]["litellm_params"]["api_key"] == "sk-no-key-required"
    assert model_list[1]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[1]["litellm_params"]["api_key"] == "sk-no-key-required"


def test_build_litellm_config_keeps_local_route_even_if_non_local_envs_are_present() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free="
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
            ),
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    model_list = _model_list(config)
    assert [entry["model_name"] for entry in model_list] == ["local/unsloth-active", "unsloth-active"]
    assert model_list[0]["litellm_params"] == {
        "model": "openai/unsloth-active",
        "api_base": "http://vllm.internal:8000/v1",
        "api_key": "sk-no-key-required",
    }
    assert model_list[1]["litellm_params"] == {
        "model": "openai/unsloth-active",
        "api_base": "http://vllm.internal:8000/v1",
        "api_key": "sk-no-key-required",
    }

    rendered_yaml = render_litellm_config_yaml(config)
    assert 'model_name: "local/unsloth-active"' in rendered_yaml
    assert 'model: "openai/unsloth-active"' in rendered_yaml
    assert 'api_key: "sk-no-key-required"' in rendered_yaml
    assert 'model_name: "openai/*"' not in rendered_yaml
    assert 'openrouter/nvidia/nemotron-3-super-120b-a12b:free' not in rendered_yaml


def test_build_litellm_config_still_allows_optional_nvidia_route() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
        {
            "nvidia_api_key_env": "NVIDIA_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "local/unsloth-active",
        "unsloth-active",
        "nvidia/kimi-k2.5",
    ]
    assert model_list[2]["litellm_params"] == {
        "model": "nvidia/moonshotai/kimi-k2.5",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "api_key": "os.environ/NVIDIA_API_KEY",
    }


def test_build_litellm_config_normalizes_legacy_local_model_to_openai_prefix() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth/Qwen2.5-Coder-32B-Instruct",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert model_list[0]["model_name"] == "local/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth/Qwen2.5-Coder-32B-Instruct"


def test_build_litellm_config_defaults_local_model_to_unsloth_active() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert model_list[0]["model_name"] == "local/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[0]["litellm_params"]["api_key"] == "sk-no-key-required"


def test_build_litellm_config_allows_local_api_key_override() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-local-override",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert model_list[0]["model_name"] == "local/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[0]["litellm_params"]["api_key"] == "sk-local-override"
