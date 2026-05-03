# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any, cast

from dokploy_wizard.litellm.config_renderer import build_litellm_config, render_litellm_config_yaml


def _model_list(config: dict[str, object]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], config["model_list"])


def test_build_litellm_config_orders_local_then_opencode_then_explicit_aliases() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth/Qwen2.5-Coder-32B-Instruct",
            "AI_DEFAULT_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
                "openrouter/healer-alpha=openrouter/anthropic/claude-3.5-sonnet"
            ),
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "local/unsloth-active",
        "openai/*",
        "openrouter/hunter-alpha",
        "openrouter/healer-alpha",
    ]

    wildcard_entries = [entry for entry in model_list if "*" in entry["model_name"]]
    assert wildcard_entries == [model_list[1]]
    assert wildcard_entries[0]["litellm_params"]["model"] == "openai/*"
    assert wildcard_entries[0]["litellm_params"]["api_base"] == "https://opencode.ai/zen/go/v1"
    assert wildcard_entries[0]["litellm_params"]["api_key"] == "os.environ/OPENCODE_GO_API_KEY"

    assert all(entry["model_name"] != "openrouter/*" for entry in model_list)
    assert all(entry["litellm_params"]["model"] != "openrouter/*" for entry in model_list[2:])


def test_build_litellm_config_uses_env_refs_and_includes_optional_nvidia_route() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENROUTER_MODELS": "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
            "nvidia_api_key_env": "NVIDIA_API_KEY",
        },
    )

    model_list = _model_list(config)
    nvidia_entry = next(entry for entry in model_list if entry["model_name"] == "nvidia/kimi-k2.5")
    openrouter_entry = next(
        entry for entry in model_list if entry["model_name"] == "openrouter/hunter-alpha"
    )

    assert nvidia_entry["litellm_params"] == {
        "model": "nvidia/moonshotai/kimi-k2.5",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "api_key": "os.environ/NVIDIA_API_KEY",
    }
    assert openrouter_entry["litellm_params"]["api_key"] == "os.environ/OPENROUTER_API_KEY"

    rendered_yaml = render_litellm_config_yaml(config)
    assert "os.environ/OPENCODE_GO_API_KEY" in rendered_yaml
    assert "os.environ/OPENROUTER_API_KEY" in rendered_yaml
    assert "os.environ/NVIDIA_API_KEY" in rendered_yaml
    assert "OPENROUTER_API_KEY=" not in rendered_yaml
    assert "NVIDIA_API_KEY=" not in rendered_yaml
