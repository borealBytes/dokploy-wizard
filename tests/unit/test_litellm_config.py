# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any, cast

import pytest

from dokploy_wizard.litellm.config_renderer import build_litellm_config, render_litellm_config_yaml


def _model_list(config: dict[str, object]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], config["model_list"])


def _model_entry(config: dict[str, object], model_name: str) -> dict[str, Any]:
    for entry in _model_list(config):
        if entry["model_name"] == model_name:
            return entry
    raise AssertionError(f"model entry not found: {model_name}")


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
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
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
                "LITELLM_OPENROUTER_MODELS": "*",
            },
            {
                "openrouter_api_key_env": "OPENROUTER_API_KEY",
            },
        )


def test_build_litellm_config_exposes_verified_opencode_go_chat_models_only_when_key_present(
) -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_names = [entry["model_name"] for entry in _model_list(config)]

    assert model_names == [
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        "opencode-go/deepseek-v4-flash",
        "opencode-go/gpt-4.1-mini",
    ]
    assert "deepseek-v4-flash" not in model_names
    assert "opencode-go/text-embedding-3-large" not in model_names

    deepseek_entry = _model_entry(config, "opencode-go/deepseek-v4-flash")
    assert deepseek_entry["litellm_params"] == {
        "model": "openai/deepseek-v4-flash",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/OPENCODE_GO_API_KEY",
    }


def test_build_litellm_config_skips_opencode_go_models_without_upstream_key() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {},
    )

    assert [entry["model_name"] for entry in _model_list(config)] == [
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    ]


def test_render_litellm_config_yaml_includes_new_model_aliases_only() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_OPENROUTER_MODELS": "minimax/minimax-m2.5:free",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            "openrouter_api_key_env": "OPENROUTER_API_KEY",
        },
    )

    rendered_yaml = render_litellm_config_yaml(config)

    assert 'model_name: "tuxdesktop.tailb12aa5.ts.net/unsloth-active"' in rendered_yaml
    assert 'model_name: "opencode-go/deepseek-v4-flash"' in rendered_yaml
    assert 'model_name: "openrouter/minimax/minimax-m2.5:free"' in rendered_yaml
    assert 'model_name: "openai/*"' not in rendered_yaml
    assert 'model_name: "minimax/minimax-m2.5:free"' not in rendered_yaml


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
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        "nvidia/kimi-k2.5",
    ]
    assert model_list[1]["litellm_params"] == {
        "model": "nvidia/moonshotai/kimi-k2.5",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "api_key": "os.environ/NVIDIA_API_KEY",
    }


def test_render_litellm_config_yaml_preserves_nvidia_alias_routes() -> None:
    config = build_litellm_config(
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
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
    assert 'model: "nvidia/moonshotai/kimi-k2.5"' in rendered_yaml
    assert 'api_base: "https://integrate.api.nvidia.com/v1"' in rendered_yaml
    assert 'model_name: "openrouter/google/gemma-4-31b-it:free"' in rendered_yaml
    assert 'model_name: "openrouter/nvidia/kimi-k2.5"' not in rendered_yaml
    assert 'model: "openrouter/nvidia/moonshotai/kimi-k2.5"' not in rendered_yaml


@pytest.mark.parametrize(
    ("raw_value", "message"),
    [
        ("nvidia/kimi-k2.5", "Expected alias=model format for LITELLM_NVIDIA_MODELS"),
        (
            "nvidia/kimi-k2.5=",
            "Expected non-empty alias=model format for LITELLM_NVIDIA_MODELS",
        ),
        (
            "=nvidia/moonshotai/kimi-k2.5",
            "Expected non-empty alias=model format for LITELLM_NVIDIA_MODELS",
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
                "LITELLM_NVIDIA_MODELS": raw_value,
                "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
            },
            {
                "nvidia_api_key_env": "NVIDIA_API_KEY",
            },
        )


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

    assert model_list[0]["model_name"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
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

    assert model_list[0]["model_name"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
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

    assert model_list[0]["model_name"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    assert model_list[0]["litellm_params"]["model"] == "openai/unsloth-active"
    assert model_list[0]["litellm_params"]["api_key"] == "sk-local-override"


def test_build_litellm_config_preserves_hostname_provider_alias_for_local_route() -> None:
    config = build_litellm_config(
        {
            "AI_DEFAULT_PROVIDER": "tuxdesktop.tailb12aa5.ts.net",
            "AI_DEFAULT_MODEL": "unsloth-active",
            "LITELLM_LOCAL_BASE_URL": "http://tuxdesktop.tailb12aa5.ts.net:61434/v1",
        },
        {
            "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )

    model_list = _model_list(config)

    assert [entry["model_name"] for entry in model_list] == [
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    ]
    assert model_list[0]["litellm_params"] == {
        "model": "openai/unsloth-active",
        "api_base": "http://tuxdesktop.tailb12aa5.ts.net:61434/v1",
        "api_key": "sk-no-key-required",
    }

    rendered_yaml = render_litellm_config_yaml(config)
    assert 'model_name: "tuxdesktop.tailb12aa5.ts.net/unsloth-active"' in rendered_yaml
    assert 'model: "openai/unsloth-active"' in rendered_yaml
    assert 'model: "openai/tuxdesktop.tailb12aa5.ts.net/unsloth-active"' not in rendered_yaml
    assert 'model_name: "unsloth-active"' not in rendered_yaml


def test_build_litellm_config_rejects_mangled_hostname_provider_upstream_target() -> None:
    with pytest.raises(
        ValueError,
        match=r"openai/tuxdesktop\.tailb12aa5\.ts\.net/unsloth-active",
    ):
        build_litellm_config(
            {
                "LITELLM_LOCAL_BASE_URL": "http://tuxdesktop.tailb12aa5.ts.net:61434/v1",
                "LITELLM_LOCAL_MODEL": "openai/tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            },
            {
                "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
            },
        )
