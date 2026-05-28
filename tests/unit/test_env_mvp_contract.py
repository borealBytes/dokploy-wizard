# mypy: ignore-errors
# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any, cast

import pytest

import dokploy_wizard.lifecycle.changes as change_module
import dokploy_wizard.networking.cloudflare as cloudflare_module
import dokploy_wizard.networking.planner as networking_planner
import dokploy_wizard.state.env as env_module
from dokploy_wizard import cli
from dokploy_wizard.packs.catalog import get_mutable_pack_env_keys, get_pack_definition
from dokploy_wizard.packs.resolver import resolve_pack_selection
from dokploy_wizard.state import resolve_desired_state
from dokploy_wizard.state.models import RawEnvInput, StateValidationError

_ALWAYS_REQUIRED_KEYS = frozenset({"ROOT_DOMAIN"})
_LIVE_BOOTSTRAP_REQUIRED_KEYS = frozenset(
    {
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ZONE_ID",
        "DOKPLOY_ADMIN_EMAIL",
        "DOKPLOY_ADMIN_PASSWORD",
    }
)
_ENABLED_FEATURE_REQUIRED_KEYS = frozenset({"TAILSCALE_AUTH_KEY", "TAILSCALE_HOSTNAME"})
_GENERATED_STATE_BACKED_KEYS = frozenset(
    {
        "DOKPLOY_API_KEY",
        "OPENCLAW_GATEWAY_PASSWORD",
        "OPENCLAW_GATEWAY_TOKEN",
        "SEAWEEDFS_ACCESS_KEY",
        "SEAWEEDFS_SECRET_KEY",
    }
)
_DEFAULTED_KEYS = frozenset(
    {
        "AI_DEFAULT_PROVIDER",
        "AI_DEFAULT_MODEL",
        "STACK_NAME",
        "DOKPLOY_SUBDOMAIN",
        "LITELLM_ADMIN_SUBDOMAIN",
        "NEXTCLOUD_SUBDOMAIN",
        "ONLYOFFICE_SUBDOMAIN",
        "MY_FARM_ADVISOR_CHANNELS",
    }
)
_OPTIONAL_COMMENTED_KEYS = frozenset(
    {
        "DOCKER_USERNAME",
        "DOCKER_PAT",
        "ENABLE_TAILSCALE",
        "TAILSCALE_AUTH_KEY",
        "TAILSCALE_HOSTNAME",
        "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
        "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
        "R2_BUCKET_NAME",
        "R2_ENDPOINT",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "CF_ACCOUNT_ID",
        "LITELLM_LOCAL_BASE_URL",
        "LITELLM_LOCAL_MODEL",
        "LITELLM_LOCAL_API_KEY",
        "LITELLM_NVIDIA_API_KEY",
        "LITELLM_NVIDIA_BASE_URL",
    }
)
_DEFAULT_AI_PROVIDER = "openrouter"
_DEFAULT_AI_MODEL = "deepseek/deepseek-v4-flash:free"


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _load_install_min_env_shape() -> tuple[
    frozenset[str], dict[str, tuple[bool, bool]], dict[str, str]
]:
    active_keys: set[str] = set()
    model_shape: dict[str, tuple[bool, bool]] = {}
    safe_values: dict[str, str] = {}
    safe_value_keys = {"PACKS", "AI_DEFAULT_PROVIDER", "AI_DEFAULT_MODEL"}
    for raw_line in (_repo_root() / ".install-min.env").read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        active_keys.add(key)
        if key in safe_value_keys:
            safe_values[key] = value.strip()
        if key == "LITELLM_OPENROUTER_MODELS":
            model_shape[key] = ("/" in value, ":" in value)
    return frozenset(active_keys), model_shape, safe_values


def _load_active_env_keys(relative_path: str) -> frozenset[str]:
    active_keys: set[str] = set()
    for raw_line in (_repo_root() / relative_path).read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _value = stripped.split("=", 1)
        active_keys.add(key.strip())
    return frozenset(active_keys)


def _minimal_env(**overrides: str) -> RawEnvInput:
    values = {
        "ROOT_DOMAIN": "example.test",
        "PACKS": "nextcloud,coder",
    }
    values.update(overrides)
    return RawEnvInput(format_version=1, values=values)


def _farm_litellm_env(**overrides: str) -> RawEnvInput:
    values = {
        "ROOT_DOMAIN": "example.com",
        "PACKS": "my-farm-advisor",
        "LITELLM_IMAGE": "ghcr.io/berriai/litellm",
        "LITELLM_IMAGE_TAG": "main-v1.40.14-stable",
        "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
        "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        "LITELLM_ADMIN_SUBDOMAIN": "litellm",
        "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
        "LITELLM_OPENROUTER_MODELS": (
            "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
            "openrouter/healer-alpha=openrouter/anthropic/claude-3.5-sonnet"
        ),
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
        "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
    }
    values.update(overrides)
    return RawEnvInput(format_version=1, values=values)


def test_minimal_env_contract_key_categories_are_explicit_and_secret_free() -> None:
    install_min_keys, model_shape, _safe_values = _load_install_min_env_shape()
    categorized_keys = (
        _ALWAYS_REQUIRED_KEYS
        | _LIVE_BOOTSTRAP_REQUIRED_KEYS
        | _ENABLED_FEATURE_REQUIRED_KEYS
        | _GENERATED_STATE_BACKED_KEYS
        | _DEFAULTED_KEYS
        | _OPTIONAL_COMMENTED_KEYS
    )

    assert {"ROOT_DOMAIN", "PACKS"} <= install_min_keys
    assert {
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ZONE_ID",
    } <= install_min_keys
    assert {"DOKPLOY_ADMIN_EMAIL", "DOKPLOY_ADMIN_PASSWORD"} <= install_min_keys
    assert {
        "AI_DEFAULT_PROVIDER",
        "AI_DEFAULT_MODEL",
        "LITELLM_OPENROUTER_MODELS",
    } <= install_min_keys
    assert model_shape["LITELLM_OPENROUTER_MODELS"] == (True, True)
    assert _ALWAYS_REQUIRED_KEYS <= categorized_keys
    assert _GENERATED_STATE_BACKED_KEYS.isdisjoint(_LIVE_BOOTSTRAP_REQUIRED_KEYS)
    assert _DEFAULT_AI_PROVIDER == "openrouter"
    assert _DEFAULT_AI_MODEL == "deepseek/deepseek-v4-flash:free"


def test_install_min_env_safe_shape_matches_minimized_remote_proof_stack() -> None:
    install_min_keys, model_shape, safe_values = _load_install_min_env_shape()
    active_packs = safe_values["PACKS"].split(",")
    required_proof_packs = ["nextcloud", "my-farm-advisor", "seaweedfs", "coder"]
    optional_proof_packs = {"surfsense"}

    assert active_packs[: len(required_proof_packs)] == required_proof_packs
    assert len(active_packs) == len(set(active_packs))
    assert set(active_packs).issubset(set(required_proof_packs) | optional_proof_packs)
    assert safe_values["AI_DEFAULT_PROVIDER"] == _DEFAULT_AI_PROVIDER
    assert safe_values["AI_DEFAULT_MODEL"] == _DEFAULT_AI_MODEL
    assert model_shape["LITELLM_OPENROUTER_MODELS"] == (True, True)
    assert _GENERATED_STATE_BACKED_KEYS.isdisjoint(install_min_keys)
    assert "MY_FARM_ADVISOR_CHANNELS" not in install_min_keys


def test_minimal_env_uses_packs_for_enablement_and_defaults_optional_hostnames() -> None:
    raw_env = _minimal_env(PACKS="nextcloud,coder")
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)

    assert "ENABLE_NEXTCLOUD" not in raw_env.values
    assert "ENABLE_CODER" not in raw_env.values
    assert "STACK_NAME" not in raw_env.values
    assert "DOKPLOY_SUBDOMAIN" not in raw_env.values
    assert selection.selected_packs == ("coder", "nextcloud")
    assert desired_state.stack_name == "example-test"
    assert desired_state.shared_core.network_name == "example-test-shared"
    assert desired_state.enabled_packs == ("coder", "nextcloud")
    assert desired_state.enable_tailscale is False
    assert desired_state.tailscale_hostname is None
    assert desired_state.hostnames["coder"] == "coder.example.test"
    assert desired_state.hostnames["coder-wildcard"] == "*.example.test"
    assert desired_state.hostnames["nextcloud"] == "nextcloud.example.test"
    assert desired_state.hostnames["onlyoffice"] == "office.example.test"
    assert desired_state.hostnames["dokploy"] == "dokploy.example.test"


def test_stack_name_defaults_from_root_domain_and_preserves_explicit_override() -> None:
    default_state = resolve_desired_state(_minimal_env(ROOT_DOMAIN="openmerge.me"))
    blank_state = resolve_desired_state(
        _minimal_env(ROOT_DOMAIN="openmerge.me", STACK_NAME="  ")
    )
    explicit_state = resolve_desired_state(
        _minimal_env(ROOT_DOMAIN="openmerge.me", STACK_NAME="custom-stack")
    )

    assert default_state.stack_name == "openmerge-me"
    assert blank_state.stack_name == "openmerge-me"
    assert explicit_state.stack_name == "custom-stack"
    assert default_state.shared_core.network_name == "openmerge-me-shared"
    assert default_state.shared_core.litellm is not None
    assert default_state.shared_core.litellm.service_name == "openmerge-me-shared-litellm"
    assert default_state.dokploy_url == "https://dokploy.openmerge.me"


def test_subdomain_defaults_are_optional_and_overrides_are_honored() -> None:
    default_state = resolve_desired_state(_farm_litellm_env())
    override_state = resolve_desired_state(
        _farm_litellm_env(
            DOKPLOY_SUBDOMAIN="control",
            MY_FARM_ADVISOR_SUBDOMAIN="advisor",
        )
    )

    assert default_state.hostnames["dokploy"] == "dokploy.example.com"
    assert default_state.dokploy_url == "https://dokploy.example.com"
    assert default_state.hostnames["my-farm-advisor"] == "farm.example.com"
    assert override_state.hostnames["dokploy"] == "control.example.com"
    assert override_state.dokploy_url == "https://control.example.com"
    assert override_state.hostnames["my-farm-advisor"] == "advisor.example.com"


def test_public_examples_do_not_make_default_subdomains_active_inputs() -> None:
    for relative_path in (".install.env.example", ".install-my-farm-advisor.env.example"):
        active_keys = _load_active_env_keys(relative_path)
        assert "STACK_NAME" not in active_keys
        assert "DOKPLOY_SUBDOMAIN" not in active_keys
        assert "MY_FARM_ADVISOR_SUBDOMAIN" not in active_keys
        assert "MY_FARM_ADVISOR_CHANNELS" not in active_keys


@pytest.mark.parametrize(
    "channel_overrides",
    [
        {},
        {"MY_FARM_ADVISOR_CHANNELS": ""},
        {"MY_FARM_ADVISOR_CHANNELS": " , "},
    ],
)
def test_my_farm_advisor_channels_default_when_omitted_or_blank(
    channel_overrides: dict[str, str],
) -> None:
    raw_env = _farm_litellm_env(**channel_overrides)
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)

    assert "my-farm-advisor" in desired_state.enabled_packs
    assert selection.my_farm_advisor_channels == ("telegram",)
    assert desired_state.my_farm_advisor_channels == ("telegram",)


def test_omitted_optional_integrations_pass_when_features_are_disabled_or_unconfigured() -> None:
    raw_env = _minimal_env(PACKS="nextcloud,coder")
    desired_state = resolve_desired_state(raw_env)

    assert cli._docker_hub_credentials_from_env(raw_env) is None
    assert desired_state.enable_tailscale is False
    assert desired_state.tailscale_tags == ()
    assert desired_state.tailscale_subnet_routes == ()
    assert "my-farm-advisor" not in desired_state.enabled_packs
    assert "LITELLM_LOCAL_BASE_URL" not in raw_env.values
    assert "LITELLM_NVIDIA_API_KEY" not in raw_env.values
    assert _OPTIONAL_COMMENTED_KEYS.isdisjoint(raw_env.values)


@pytest.mark.parametrize("key", sorted(_ALWAYS_REQUIRED_KEYS))
def test_missing_always_required_keys_fail_with_clear_messages(key: str) -> None:
    raw_env = _minimal_env()
    values = dict(raw_env.values)
    values.pop(key)

    with pytest.raises(StateValidationError, match=rf"Missing required env key '{key}'"):
        resolve_desired_state(RawEnvInput(format_version=1, values=values))


def test_missing_cloudflare_api_token_fails_before_live_api_calls() -> None:
    raw_env = _minimal_env(CLOUDFLARE_ACCOUNT_ID="account-id", CLOUDFLARE_ZONE_ID="zone-id")

    with pytest.raises(
        StateValidationError,
        match="Missing required env key 'CLOUDFLARE_API_TOKEN'",
    ):
        cloudflare_module.CloudflareApiBackend(raw_env)


def test_missing_cloudflare_account_id_fails_with_clear_message() -> None:
    raw_env = _minimal_env(CLOUDFLARE_API_TOKEN="cf-token", CLOUDFLARE_ZONE_ID="zone-id")

    with pytest.raises(
        StateValidationError,
        match="Missing required env key 'CLOUDFLARE_ACCOUNT_ID'",
    ):
        networking_planner._resolve_credentials(
            raw_env,
            resolve_desired_state(_minimal_env()),
            backend=cast(Any, object()),
        )


def test_missing_cloudflare_zone_id_requires_resolvable_root_domain() -> None:
    class NoZoneBackend:
        def resolve_zone_id(self, account_id: str, root_domain: str) -> None:
            assert account_id == "account-id"
            assert root_domain == "example.test"
            return None

    raw_env = _minimal_env(CLOUDFLARE_ACCOUNT_ID="account-id", CLOUDFLARE_API_TOKEN="cf-token")

    with pytest.raises(
        cloudflare_module.CloudflareError,
        match="Cloudflare could not find a matching zone for the root domain",
    ):
        networking_planner._resolve_credentials(
            raw_env,
            resolve_desired_state(_minimal_env()),
            backend=cast(Any, NoZoneBackend()),
        )


def test_missing_dokploy_admin_auth_fails_for_real_bootstrap(tmp_path) -> None:
    raw_env = _minimal_env()

    with pytest.raises(StateValidationError, match="Dokploy admin email/password are required"):
        cli._ensure_dokploy_api_auth(
            env_file=tmp_path / "install.env",
            raw_env=raw_env,
            desired_state=resolve_desired_state(raw_env),
            bootstrap_backend=cast(Any, object()),
            dry_run=False,
            require_real_dokploy_auth=True,
        )


def test_generated_state_backed_pack_secrets_are_not_operator_required() -> None:
    desired_state = resolve_desired_state(_minimal_env(PACKS="seaweedfs"))

    assert desired_state.seaweedfs_access_key is None
    assert desired_state.seaweedfs_secret_key is None


def test_dokploy_admin_password_is_required_when_email_is_set_for_real_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class RefreshedApiKey:
        api_key = "generated-api-key"

    monkeypatch.setattr(cli, "reconcile_dokploy", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_refresh_local_dokploy_api_key",
        lambda **_kwargs: RefreshedApiKey(),
    )
    raw_env = _minimal_env(DOKPLOY_ADMIN_EMAIL="admin@example.test")

    with pytest.raises(StateValidationError, match="DOKPLOY_ADMIN_PASSWORD"):
        cli._ensure_dokploy_api_auth(
            env_file=tmp_path / "install.env",
            raw_env=raw_env,
            desired_state=resolve_desired_state(raw_env),
            bootstrap_backend=cast(Any, object()),
            dry_run=False,
            require_real_dokploy_auth=True,
        )


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


def test_my_farm_advisor_can_be_enabled_by_packs_without_duplicate_flag() -> None:
    raw_env = _farm_litellm_env()
    desired_state = resolve_desired_state(raw_env)

    assert raw_env.values["PACKS"] == "my-farm-advisor"
    assert "ENABLE_MY_FARM_ADVISOR" not in raw_env.values
    assert "MY_FARM_ADVISOR_SUBDOMAIN" not in raw_env.values
    assert "MY_FARM_ADVISOR_CHANNELS" not in raw_env.values
    assert desired_state.selected_packs == ("my-farm-advisor",)
    assert desired_state.enabled_packs == ("my-farm-advisor",)
    assert desired_state.hostnames["my-farm-advisor"] == "farm.example.com"
    assert desired_state.my_farm_advisor_channels == ("telegram",)


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


def test_ai_default_provider_and_model_resolve_local_alias_but_requires_local_base_url() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        AI_DEFAULT_PROVIDER="local-model.internal",
        AI_DEFAULT_MODEL="unsloth-active",
    )

    assert env_module.resolve_ai_default_model_ref(raw_env.values) == (
        "local-model.internal/unsloth-active"
    )
    with pytest.raises(StateValidationError, match="LITELLM_LOCAL_BASE_URL"):
        resolve_desired_state(raw_env)


def test_ai_default_provider_and_model_resolve_opencode_alias() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        LITELLM_LOCAL_MODEL="",
        LITELLM_LOCAL_API_KEY="",
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
        LITELLM_LOCAL_MODEL="",
        LITELLM_LOCAL_API_KEY="",
        LITELLM_OPENROUTER_API_KEY="SECRET_TEST_OPENROUTER_PROVIDER_KEY",
        AI_DEFAULT_PROVIDER="openrouter",
        AI_DEFAULT_MODEL="openai/gpt-4.1-mini",
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


def test_litellm_nvidia_models_accept_user_style_raw_model_ids() -> None:
    raw_env = _farm_litellm_env(
        MY_FARM_ADVISOR_OPENROUTER_API_KEY="",
        MY_FARM_ADVISOR_NVIDIA_API_KEY="",
        ANTHROPIC_API_KEY="",
        AI_DEFAULT_API_KEY="",
        AI_DEFAULT_BASE_URL="",
        LITELLM_LOCAL_BASE_URL="",
        LITELLM_LOCAL_MODEL="",
        LITELLM_LOCAL_API_KEY="",
        LITELLM_NVIDIA_API_KEY="fake-nvidia-key",
        LITELLM_NVIDIA_BASE_URL="https://integrate.api.nvidia.example.com/v1",
        LITELLM_NVIDIA_MODELS=(
            "moonshotai/kimi-k2.6,"
            "deepseek-ai/deepseek-v4-flash,"
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
        ),
        AI_DEFAULT_PROVIDER="nvidia",
        AI_DEFAULT_MODEL="moonshotai/kimi-k2.6",
    )

    assert env_module.parse_litellm_nvidia_models(raw_env.values) == (
        ("nvidia/moonshotai/kimi-k2.6", "nvidia/moonshotai/kimi-k2.6"),
        (
            "nvidia/deepseek-ai/deepseek-v4-flash",
            "nvidia/deepseek-ai/deepseek-v4-flash",
        ),
        (
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
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
    assert "sk-" + "or-v1-" not in readme
    assert "sk-" + "ant-" not in readme
    assert "nv" + "api-" not in readme
