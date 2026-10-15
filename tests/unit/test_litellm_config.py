# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any, cast

import pytest

from dokploy_wizard.litellm.config_renderer import build_litellm_config, render_litellm_config_yaml
from dokploy_wizard.litellm.model_sync import OPENCODE_GO_GATEWAY_EXTRA_HEADERS

_EXPECTED_OPENCODE_GO_GATEWAY_HEADERS = dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS)


def _model_list(config: dict[str, object]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], config["model_list"])


def _model_entry(config: dict[str, object], model_name: str) -> dict[str, Any]:
    for entry in _model_list(config):
        if entry["model_name"] == model_name:
            return entry
    raise AssertionError(f"model entry not found: {model_name}")


def test_build_litellm_config_omits_local_alias_when_local_base_url_absent() -> None:
    config = build_litellm_config(
        {
            "AI_DEFAULT_PROVIDER": "openrouter",
            "AI_DEFAULT_MODEL": "deepseek/deepseek-v4-flash:free",
            "LITELLM_OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
            "LITELLM_OPENROUTER_MODELS": "deepseek/deepseek-v4-flash:free",
        },
        {"openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY"},
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    assert model_names == ["openrouter/deepseek/deepseek-v4-flash:free"]
    assert "local-model.internal/unsloth-active" not in model_names



def test_build_litellm_config_maps_raw_openrouter_ids_to_prefixed_aliases() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENROUTER_MODELS": (
                "minimax/minimax-m2.5:free,"
                "google/gemma-4-31b-it:free"
            ),
        },
        {
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
            "openrouter_model_metadata": {
                "minimax/minimax-m2.5:free": {
                    "pricing": {
                        "prompt": "0.00000028",
                        "completion": "0.0000011",
                    }
                },
                "google/gemma-4-31b-it:free": {
                    "pricing": {}
                },
            },
        },
    )

    model_list = _model_list(config)
    model_names = [entry["model_name"] for entry in model_list]

    assert model_names == [
        "local-model.internal/unsloth-active",
        "openrouter/minimax/minimax-m2.5:free",
        "openrouter/google/gemma-4-31b-it:free",
    ]
    assert "minimax/minimax-m2.5:free" not in model_names

    minimax_entry = _model_entry(config, "openrouter/minimax/minimax-m2.5:free")
    assert minimax_entry["litellm_params"] == {
        "model": "openrouter/minimax/minimax-m2.5:free",
        "api_key": "os.environ/OPENROUTER_API_KEY",
    }
    assert minimax_entry["model_info"] == {
        "input_cost_per_token": 0.00000028,
        "output_cost_per_token": 0.0000011,
    }

    unknown_cost_entry = _model_entry(config, "openrouter/google/gemma-4-31b-it:free")
    assert unknown_cost_entry["litellm_params"] == {
        "model": "openrouter/google/gemma-4-31b-it:free",
        "api_key": "os.environ/OPENROUTER_API_KEY",
    }
    assert "model_info" not in unknown_cost_entry


def test_build_litellm_config_rejects_openrouter_wildcards() -> None:
    with pytest.raises(ValueError, match="OpenRouter wildcard routes are not allowed"):
        build_litellm_config(
            {
                "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
                "LITELLM_LOCAL_MODEL": "unsloth-active",
                "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
                "LITELLM_OPENROUTER_MODELS": "*",
            },
            {
                "openrouter_api_key_env": "OPENROUTER_API_KEY",
            },
        )


def test_build_litellm_config_skips_empty_openrouter_entries_to_match_env_contract() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/openai/gpt-4.1-mini, , openrouter/anthropic/claude-3.5-sonnet,"
            ),
        },
        {
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    assert [entry["model_name"] for entry in _model_list(config)] == [
        "local-model.internal/unsloth-active",
        "openrouter/openai/gpt-4.1-mini",
        "openrouter/anthropic/claude-3.5-sonnet",
    ]


def test_build_litellm_config_exposes_default_opencode_go_alias_only_when_key_present(
) -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    # Only the default alias route is static; the full catalog is autosynced.
    assert model_names == [
        "local-model.internal/unsloth-active",
        "opencode-go/deepseek-v4-flash",
    ]
    assert "deepseek-v4-flash" not in model_names
    assert "opencode-go/text-embedding-3-large" not in model_names
    assert "opencode-go/gpt-4.1-mini" not in model_names
    assert "opencode-go/minimax-m2.7" not in model_names

    default_entry = _model_entry(config, "opencode-go/deepseek-v4-flash")
    assert default_entry["litellm_params"] == {
        "model": "openai/deepseek-v4-flash",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/OPENCODE_GO_API_KEY",
        "extra_headers": _EXPECTED_OPENCODE_GO_GATEWAY_HEADERS,
    }


def test_build_litellm_config_skips_opencode_go_models_without_upstream_key() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {},
    )

    assert [entry["model_name"] for entry in _model_list(config)] == [
        "local-model.internal/unsloth-active",
    ]


def test_build_litellm_config_exposes_default_opencode_go_alias_with_default_base_url() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENCODE_GO_API_KEY": "sk-opencode-go-key",
        },
        {},
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    assert model_names == [
        "local-model.internal/unsloth-active",
        "opencode-go/deepseek-v4-flash",
    ]
    assert _model_entry(config, "opencode-go/deepseek-v4-flash")["litellm_params"] == {
        "model": "openai/deepseek-v4-flash",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/LITELLM_OPENCODE_GO_API_KEY",
        "extra_headers": _EXPECTED_OPENCODE_GO_GATEWAY_HEADERS,
    }


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", "yes", "on"])
def test_build_litellm_config_ignores_retired_opencode_go_wildcard_flag(
    raw_value: str,
) -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENCODE_GO_API_KEY": "sk-opencode-go-key",
            "LITELLM_OPENCODE_GO_WILDCARD": raw_value,
        },
        {},
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    assert model_names == [
        "local-model.internal/unsloth-active",
        "opencode-go/deepseek-v4-flash",
    ]
    assert _model_entry(config, "opencode-go/deepseek-v4-flash")["litellm_params"] == {
        "model": "openai/deepseek-v4-flash",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/LITELLM_OPENCODE_GO_API_KEY",
        "extra_headers": _EXPECTED_OPENCODE_GO_GATEWAY_HEADERS,
    }
    assert "opencode-go/*" not in model_names
    assert "opencode-go/minimax-m2.7" not in model_names
    assert "opencode-go/text-embedding-3-large" not in model_names
    assert "opencode-go/dall-e-3" not in model_names
    assert "opencode-go/whisper-1" not in model_names
    assert "opencode-go/sora-2" not in model_names
    assert "opencode-go/gpt-image-1.5" not in model_names


def test_build_litellm_config_forwards_client_session_headers_to_upstreams() -> None:
    # OpenCode Go rejects requests without x-opencode-session; the gateway
    # forwards real client session ids and pins a stable fallback header on
    # every OpenCode Go deployment so generic clients never hit 400s.
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENCODE_GO_API_KEY": "sk-opencode-go-key",
        },
        {},
    )

    general_settings = config["general_settings"]
    assert isinstance(general_settings, dict)
    assert general_settings["forward_client_headers_to_llm_api"] is True
    zen_params = _model_entry(config, "opencode-go/deepseek-v4-flash")["litellm_params"]
    assert zen_params["extra_headers"] == _EXPECTED_OPENCODE_GO_GATEWAY_HEADERS


def test_build_litellm_config_prefers_canonical_litellm_env_refs_for_non_local_routes() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENCODE_GO_API_KEY": "sk-opencode-go-key",
            "LITELLM_OPENROUTER_API_KEY": "sk-openrouter-key",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini"
            ),
        },
        {},
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    assert "openrouter/hunter-alpha" in model_names
    assert "openrouter/openai/gpt-4.1-mini" not in model_names

    default_entry = _model_entry(config, "opencode-go/deepseek-v4-flash")
    assert default_entry["litellm_params"]["api_key"] == "os.environ/LITELLM_OPENCODE_GO_API_KEY"

    openrouter_entry = _model_entry(config, "openrouter/hunter-alpha")
    assert openrouter_entry["litellm_params"] == {
        "model": "openrouter/openai/gpt-4.1-mini",
        "api_key": "os.environ/LITELLM_OPENROUTER_API_KEY",
    }


def test_render_litellm_config_yaml_includes_new_model_aliases_only() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENROUTER_MODELS": "minimax/minimax-m2.5:free",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    rendered_yaml = render_litellm_config_yaml(config)

    assert 'model_name: "local-model.internal/unsloth-active"' in rendered_yaml
    assert 'model_name: "opencode-go/deepseek-v4-flash"' in rendered_yaml
    assert 'model_name: "opencode-go/minimax-m2.7"' not in rendered_yaml
    assert 'model_name: "openrouter/minimax/minimax-m2.5:free"' in rendered_yaml
    assert 'model: "openai/*"' not in rendered_yaml
    assert 'model_name: "openai/*"' not in rendered_yaml
    assert 'model_name: "opencode-go/*"' not in rendered_yaml
    assert 'model_name: "minimax/minimax-m2.5:free"' not in rendered_yaml


def test_build_litellm_config_still_allows_optional_nvidia_route() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
        {
            "nvidia_api_key_env": "NVIDIA_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "local-model.internal/unsloth-active",
        "nvidia/kimi-k2.5",
    ]
    assert model_list[1]["litellm_params"] == {
        "model": "openai/moonshotai/kimi-k2.5",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "api_key": "os.environ/NVIDIA_API_KEY",
    }


def test_build_litellm_config_uses_canonical_nvidia_env_from_flat_raw_models() -> None:
    config = build_litellm_config(
        {
            "LITELLM_NVIDIA_API_KEY": "fake-nvidia-key",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.example.com/v1",
            "LITELLM_NVIDIA_MODELS": (
                "moonshotai/kimi-k2.6,"
                "deepseek-ai/deepseek-v4-flash,"
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
            ),
        },
        {},
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "nvidia/moonshotai/kimi-k2.6",
        "nvidia/deepseek-ai/deepseek-v4-flash",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    ]
    assert model_list[0]["litellm_params"] == {
        "model": "openai/moonshotai/kimi-k2.6",
        "api_base": "https://integrate.api.nvidia.example.com/v1",
        "api_key": "os.environ/LITELLM_NVIDIA_API_KEY",
        "max_tokens": 16384,
        "temperature": 1.0,
        "top_p": 1.0,
        "stream": True,
        "extra_body": {"chat_template_kwargs": {"thinking": True}},
    }
    assert model_list[1]["litellm_params"] == {
        "model": "openai/deepseek-ai/deepseek-v4-flash",
        "api_base": "https://integrate.api.nvidia.example.com/v1",
        "api_key": "os.environ/LITELLM_NVIDIA_API_KEY",
        "max_tokens": 16384,
        "temperature": 1.0,
        "top_p": 0.95,
        "stream": True,
        "extra_body": {
            "chat_template_kwargs": {
                "thinking": True,
                "reasoning_effort": "high",
            }
        },
    }
    assert model_list[2]["litellm_params"] == {
        "model": "openai/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "api_base": "https://integrate.api.nvidia.example.com/v1",
        "api_key": "os.environ/LITELLM_NVIDIA_API_KEY",
        "max_tokens": 65536,
        "temperature": 0.6,
        "top_p": 0.95,
        "stream": True,
        "extra_body": {
            "chat_template_kwargs": {
                "enable_thinking": True,
            },
            "reasoning_budget": 16384,
        },
    }


def test_build_litellm_config_keeps_unknown_nvidia_models_without_defaults() -> None:
    config = build_litellm_config(
        {
            "LITELLM_NVIDIA_API_KEY": "fake-nvidia-key",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.example.com/v1",
            "LITELLM_NVIDIA_MODELS": "acme/unknown-chat-model",
        },
        {},
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "nvidia/acme/unknown-chat-model",
    ]
    assert model_list[0]["litellm_params"] == {
        "model": "openai/acme/unknown-chat-model",
        "api_base": "https://integrate.api.nvidia.example.com/v1",
        "api_key": "os.environ/LITELLM_NVIDIA_API_KEY",
    }


def test_build_litellm_config_applies_nvidia_defaults_by_explicit_target_not_alias() -> None:
    config = build_litellm_config(
        {
            "LITELLM_NVIDIA_API_KEY": "fake-nvidia-key",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.example.com/v1",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-default=nvidia/moonshotai/kimi-k2.6",
        },
        {},
    )

    route = _model_entry(config, "nvidia/kimi-default")

    assert route["litellm_params"] == {
        "model": "openai/moonshotai/kimi-k2.6",
        "api_base": "https://integrate.api.nvidia.example.com/v1",
        "api_key": "os.environ/LITELLM_NVIDIA_API_KEY",
        "max_tokens": 16384,
        "temperature": 1.0,
        "top_p": 1.0,
        "stream": True,
        "extra_body": {"chat_template_kwargs": {"thinking": True}},
    }


def test_render_litellm_config_yaml_preserves_nvidia_alias_routes() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENROUTER_MODELS": "google/gemma-4-31b-it:free",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
        {
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
            "nvidia_api_key_env": "NVIDIA_API_KEY",
        },
    )

    rendered_yaml = render_litellm_config_yaml(config)

    assert 'model_name: "nvidia/kimi-k2.5"' in rendered_yaml
    assert 'model: "openai/moonshotai/kimi-k2.5"' in rendered_yaml
    assert 'api_base: "https://integrate.api.nvidia.com/v1"' in rendered_yaml
    assert 'model_name: "openrouter/google/gemma-4-31b-it:free"' in rendered_yaml
    assert 'model_name: "openrouter/nvidia/kimi-k2.5"' not in rendered_yaml
    assert 'model: "openrouter/nvidia/moonshotai/kimi-k2.5"' not in rendered_yaml


@pytest.mark.parametrize(
    ("raw_value", "message"),
    [
        (
            "nvidia/kimi-k2.5=",
            "Expected raw model ID or non-empty alias=model format for LITELLM_NVIDIA_MODELS",
        ),
        (
            "=nvidia/moonshotai/kimi-k2.5",
            "Expected raw model ID or non-empty alias=model format for LITELLM_NVIDIA_MODELS",
        ),
    ],
)
def test_build_litellm_config_rejects_malformed_nvidia_routes(
    raw_value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_litellm_config(
            {
                "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
                "LITELLM_LOCAL_MODEL": "unsloth-active",
                "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
                "LITELLM_NVIDIA_MODELS": raw_value,
                "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
            },
            {
                "nvidia_api_key_env": "NVIDIA_API_KEY",
            },
        )


def test_build_litellm_config_normalizes_legacy_local_model_to_openai_prefix() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_LOCAL_MODEL": "unsloth/Qwen2.5-Coder-32B-Instruct",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert model_list[0]["model_name"] == "local-model.internal/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth/Qwen2.5-Coder-32B-Instruct"


def test_build_litellm_config_defaults_local_model_to_unsloth_active() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert model_list[0]["model_name"] == "local-model.internal/unsloth-active"
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

    assert model_list[0]["model_name"] == "local-model.internal/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[0]["litellm_params"]["api_key"] == "sk-local-override"


def test_build_litellm_config_preserves_hostname_provider_alias_for_local_route() -> None:
    config = build_litellm_config(
        {
            "AI_DEFAULT_PROVIDER": "local-model.internal",
            "AI_DEFAULT_MODEL": "unsloth-active",
            "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    # A dotted hostname provider owns the default alias (the local route), so
    # no static OpenCode Go route is rendered even when a Go key is present.
    assert [entry["model_name"] for entry in model_list] == [
        "local-model.internal/unsloth-active",
    ]
    assert model_list[0]["litellm_params"] == {
        "model": "openai/unsloth-active",
        "api_base": "http://local-model.internal:61434/v1",
        "api_key": "sk-no-key-required",
    }

    rendered_yaml = render_litellm_config_yaml(config)
    assert 'model_name: "local-model.internal/unsloth-active"' in rendered_yaml
    assert 'model: "openai/unsloth-active"' in rendered_yaml
    assert 'model: "openai/local-model.internal/unsloth-active"' not in rendered_yaml
    assert 'model_name: "unsloth-active"' not in rendered_yaml


def test_build_litellm_config_rejects_mangled_hostname_provider_upstream_target() -> None:
    with pytest.raises(
        ValueError,
        match=r"openai/local-model\.internal/unsloth-active",
    ):
        build_litellm_config(
            {
                "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
                "LITELLM_LOCAL_MODEL": "openai/local-model.internal/unsloth-active",
                "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            },
            {
                "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            },
        )
