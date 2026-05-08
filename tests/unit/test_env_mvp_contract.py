# mypy: ignore-errors
# pyright: reportMissingImports=false

from __future__ import annotations

import pytest

import dokploy_wizard.lifecycle.changes as change_module
import dokploy_wizard.state.env as env_module
from dokploy_wizard import cli
from dokploy_wizard.packs.catalog import get_mutable_pack_env_keys, get_pack_definition
from dokploy_wizard.packs.resolver import resolve_pack_selection
from dokploy_wizard.state import resolve_desired_state
from dokploy_wizard.state.models import RawEnvInput, StateValidationError
from tests.helpers.root_install_env import root_install_env


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _load_root_env():
    return root_install_env()


def _farm_litellm_env(**overrides: str) -> RawEnvInput:
    values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "ENABLE_MY_FARM_ADVISOR": "true",
        "LITELLM_IMAGE": "ghcr.io/berriai/litellm",
        "LITELLM_IMAGE_TAG": "main-v1.40.14-stable",
        "LITELLM_LOCAL_BASE_URL": "http://tuxdesktop.tailb12aa5.ts.net:61434/v1",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
        "LITELLM_ADMIN_SUBDOMAIN": "litellm",
        "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
        "LITELLM_OPENROUTER_MODELS": (
            "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
            "openrouter/healer-alpha=openrouter/anthropic/claude-3.5-sonnet"
        ),
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
        "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
    }
    values.update(overrides)
    return RawEnvInput(format_version=1, values=values)


def test_root_install_env_resolves_current_mvp_pack_contract() -> None:
    raw_env = _load_root_env()
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)
    required_packs = {"coder", "nextcloud", "openclaw", "seaweedfs"}

    assert required_packs.issubset(set(raw_env.values["PACKS"].split(",")))
    assert required_packs.issubset(set(selection.selected_packs))
    assert required_packs.issubset(set(selection.enabled_packs))
    assert required_packs.issubset(set(desired_state.selected_packs))
    assert desired_state.enabled_packs == selection.enabled_packs
    assert desired_state.enable_tailscale is True
    assert desired_state.tailscale_hostname == "openmerge"
    assert desired_state.hostnames["coder"] == "coder.openmerge.me"
    assert desired_state.hostnames["coder-wildcard"] == "*.openmerge.me"
    assert desired_state.hostnames["nextcloud"] == "nextcloud.openmerge.me"
    assert desired_state.hostnames["onlyoffice"] == "office.openmerge.me"
    assert desired_state.hostnames["openclaw"] == "openclaw.openmerge.me"
    assert desired_state.hostnames["openclaw-internal"].endswith(".openmerge.me")
    assert desired_state.hostnames["s3"] == "s3.openmerge.me"
    assert "headscale" not in desired_state.hostnames


def test_root_install_env_keeps_headscale_disabled_while_preserving_tailscale() -> None:
    raw_env = _load_root_env()
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)

    assert raw_env.values["ENABLE_HEADSCALE"] == "false"
    assert raw_env.values["ENABLE_TAILSCALE"] == "true"
    assert "headscale" not in selection.selected_packs
    assert "headscale" not in selection.enabled_packs
    assert desired_state.selected_packs == selection.selected_packs
    assert "headscale" not in desired_state.enabled_packs
    assert desired_state.enable_tailscale is True
    assert desired_state.tailscale_hostname == "openmerge"
    assert desired_state.enabled_features == ("dokploy",)
    assert "tailscale" not in desired_state.hostnames


def test_litellm_canonical_env_validates_without_direct_consumer_provider_keys() -> None:
    desired_state = resolve_desired_state(
        _farm_litellm_env(
            MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
            MY_FARM_ADVISOR_NVIDIA_API_KEY="",
            ANTHROPIC_API_KEY="",
            AI_DEFAULT_API_KEY="",
            AI_DEFAULT_BASE_URL="",
        )
    )

    assert "my-farm-advisor" in desired_state.enabled_packs


def test_litellm_canonical_env_rejects_missing_local_endpoint_with_actionable_error() -> None:
    with pytest.raises(StateValidationError, match="LITELLM_LOCAL_BASE_URL"):
        resolve_desired_state(
            _farm_litellm_env(
                MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
                MY_FARM_ADVISOR_NVIDIA_API_KEY="",
                ANTHROPIC_API_KEY="",
                AI_DEFAULT_API_KEY="",
                AI_DEFAULT_BASE_URL="",
                LITELLM_LOCAL_BASE_URL="",
                OPENCODE_GO_API_KEY="",
                OPENCODE_GO_BASE_URL="",
            )
        )


def test_ai_default_provider_and_model_resolve_local_alias_without_shared_api_pair() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        AI_DEFAULT_PROVIDER="tuxdesktop.tailb12aa5.ts.net",
        AI_DEFAULT_MODEL="unsloth-active",
    )

    assert env_module.resolve_ai_default_model_ref(raw_env.values) == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    )
    assert "my-farm-advisor" in resolve_desired_state(raw_env).enabled_packs


def test_ai_default_provider_and_model_resolve_opencode_alias() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        AI_DEFAULT_PROVIDER="opencode",
        AI_DEFAULT_MODEL="minimax/minimax-m2.5:free",
    )

    assert env_module.resolve_ai_default_model_ref(raw_env.values) == (
        "opencode-go/minimax/minimax-m2.5:free"
    )
    assert "my-farm-advisor" in resolve_desired_state(raw_env).enabled_packs


def test_litellm_openrouter_models_accept_raw_openrouter_model_ids() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        AI_DEFAULT_PROVIDER="tuxdesktop.tailb12aa5.ts.net",
        AI_DEFAULT_MODEL="unsloth-active",
        LITELLM_OPENROUTER_MODELS=(
            "openrouter/openai/gpt-4.1-mini,"
            "openrouter/anthropic/claude-3.5-sonnet"
        ),
    )

    assert env_module.parse_litellm_openrouter_models(raw_env.values) == (
        (
            "openrouter/openai/gpt-4.1-mini",
            "openrouter/openai/gpt-4.1-mini",
        ),
        (
            "openrouter/anthropic/claude-3.5-sonnet",
            "openrouter/anthropic/claude-3.5-sonnet",
        ),
    )
    assert "my-farm-advisor" in resolve_desired_state(raw_env).enabled_packs


def test_ai_default_provider_requires_model_name() -> None:
    with pytest.raises(StateValidationError, match="AI_DEFAULT_MODEL"):
        resolve_desired_state(
            _farm_litellm_env(
                MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
                MY_FARM_ADVISOR_NVIDIA_API_KEY="",
                ANTHROPIC_API_KEY="",
                AI_DEFAULT_API_KEY="",
                AI_DEFAULT_BASE_URL="",
                LITELLM_LOCAL_BASE_URL="",
                AI_DEFAULT_PROVIDER="opencode",
                AI_DEFAULT_MODEL="",
            )
        )


def test_ai_default_model_requires_provider_name() -> None:
    with pytest.raises(StateValidationError, match="AI_DEFAULT_PROVIDER"):
        resolve_desired_state(
            _farm_litellm_env(
                MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
                MY_FARM_ADVISOR_NVIDIA_API_KEY="",
                ANTHROPIC_API_KEY="",
                AI_DEFAULT_API_KEY="",
                AI_DEFAULT_BASE_URL="",
                LITELLM_LOCAL_BASE_URL="",
                AI_DEFAULT_PROVIDER="",
                AI_DEFAULT_MODEL="unsloth-active",
            )
        )


def test_new_ai_contract_keys_are_mutable_and_sensitive() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "AI_DEFAULT_PROVIDER": "opencode",
            "AI_DEFAULT_MODEL": "minimax/minimax-m2.5:free",
            "LITELLM_OPENROUTER_API_KEY": "should-not-leak-openrouter",
            "LITELLM_OPENCODE_GO_API_KEY": "should-not-leak-opencode",
        },
    )

    redacted = cli._redacted_raw_env_input(raw_env)

    assert redacted.values["LITELLM_OPENROUTER_API_KEY"] == "<redacted>"
    assert redacted.values["LITELLM_OPENCODE_GO_API_KEY"] == "<redacted>"
    assert "AI_DEFAULT_PROVIDER" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_MODEL" in get_pack_definition("openclaw").mutable_env_keys
    assert {
        "AI_DEFAULT_PROVIDER",
        "AI_DEFAULT_MODEL",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_OPENCODE_GO_API_KEY",
    } <= set(get_mutable_pack_env_keys())
    assert {
        "AI_DEFAULT_PROVIDER",
        "AI_DEFAULT_MODEL",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_OPENCODE_GO_API_KEY",
    } <= change_module._SUPPORTED_MODIFY_KEYS
    assert {
        "AI_DEFAULT_PROVIDER",
        "AI_DEFAULT_MODEL",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_OPENCODE_GO_API_KEY",
    } <= change_module._LITELLM_MUTABLE_ENV_KEYS


def test_readme_documents_litellm_core_gateway_contract() -> None:
    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")

    assert "LiteLLM" in readme
    assert "always installed" in readme or "core infrastructure" in readme
    assert "optional" not in readme.lower().split("liteLLM")[0].split("litellm")[0][-200:].lower()
    assert ".install.env" in readme
    assert "flat" in readme.lower()
    assert "local/unsloth-active" in readme
    assert "OpenCode Go" in readme or "opencode" in readme.lower()
    assert "wildcard" in readme.lower()
    assert "explicit" in readme.lower() and "OpenRouter" in readme
    assert "virtual key" in readme.lower()
    assert "generated" in readme.lower()
    assert "stable" in readme.lower()
    assert "state" in readme.lower()
    assert "not written back" in readme.lower() or "not written" in readme.lower()
    assert "litellm." in readme
    assert "Cloudflare Access" in readme
    assert "302" in readme and "403" in readme
    assert "docker run --rm --network" in readme
    assert "tailscale ssh" in readme
    assert "migration" in readme.lower() or "migrating" in readme.lower()
    assert "direct provider" in readme.lower() or "upstream" in readme.lower()
    assert "pytest" in readme
    assert "ruff" in readme
    assert "sk-or-v1-" not in readme
    assert "sk-ant-" not in readme
    assert "nvapi-" not in readme
