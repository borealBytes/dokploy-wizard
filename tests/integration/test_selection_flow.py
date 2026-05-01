# pyright: reportMissingImports=false

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.packs import prompts as prompt_module
from dokploy_wizard.packs.prompts import (
    GuidedInstallValues,
    PromptSelection,
    apply_prompt_selection,
)
from dokploy_wizard.state import RawEnvInput, resolve_desired_state

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_env_driven_selection_flow_resolves_requested_and_expanded_packs(
    tmp_path: Path,
) -> None:
    result = _run_cli(
        "install",
        "--env-file",
        str(FIXTURES_DIR / "openclaw-telegram.env"),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    desired_state = payload["desired_state"]
    assert desired_state["selected_packs"] == ["openclaw"]
    assert desired_state["enabled_packs"] == ["openclaw"]
    assert desired_state["hostnames"]["openclaw"] == "openclaw.example.com"
    assert desired_state["openclaw_channels"] == ["telegram"]
    assert payload["preflight"]["required_profile"]["name"] == "Recommended"


def test_both_advisor_packs_can_be_selected_together(tmp_path: Path) -> None:
    env_file = tmp_path / "both-advisors.env"
    env_file.write_text(
        "\n".join(
            (
                "STACK_NAME=invalid-pack-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_OPENCLAW=true",
                "ENABLE_MY_FARM_ADVISOR=true",
                "AI_DEFAULT_API_KEY=shared-key",
                "AI_DEFAULT_BASE_URL=https://opencode.ai/zen/go/v1",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=4",
                "HOST_MEMORY_GB=8",
                "HOST_DISK_GB=100",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "CLOUDFLARE_ZONE_ID=zone-123",
                "CLOUDFLARE_TUNNEL_NAME=invalid-pack-stack-tunnel",
                "CLOUDFLARE_MOCK_ACCOUNT_OK=true",
                "CLOUDFLARE_MOCK_ZONE_OK=true",
                "",
            )
        ),
        encoding="utf-8",
    )
    result = _run_cli(
        "install",
        "--env-file",
        str(env_file),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    desired_state = payload["desired_state"]
    assert desired_state["enabled_packs"] == ["my-farm-advisor", "openclaw"]


def test_guided_install_branch_reuses_pack_selection_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_install_flow(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        raw_env = kwargs["raw_env"]
        assert isinstance(raw_env, RawEnvInput)
        return {
            "desired_state": resolve_desired_state(raw_env).to_dict(),
            "preflight": {"required_profile": {"name": "Recommended"}},
        }

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_for_guided_state_dir", lambda path: path)
    monkeypatch.setattr(
        cli,
        "prompt_for_initial_install_values",
        lambda **kwargs: GuidedInstallValues(
            stack_name="selection-stack",
            root_domain="example.com",
            dokploy_subdomain="dokploy",
            dokploy_admin_email="clayton@superiorbyteworks.com",
            dokploy_admin_password=None,
            ai_default_api_key="ai-key-123",
            ai_default_base_url="https://opencode.ai/zen/go/v1",
            enable_headscale=True,
            cloudflare_api_token="token-123",
            cloudflare_account_id="account-123",
            cloudflare_zone_id=None,
            enable_tailscale=False,
            tailscale_auth_key=None,
            tailscale_hostname=None,
            tailscale_enable_ssh=False,
            tailscale_tags=(),
            tailscale_subnet_routes=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "prompt_for_pack_selection",
        lambda **kwargs: PromptSelection(
            selected_packs=("openclaw",),
            disabled_packs=(),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={"OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated"},
            advisor_env={"OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated"},
            openclaw_channels=("telegram",),
            my_farm_advisor_channels=(),
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", fake_run_install_flow)

    result = cli._handle_install(
        Namespace(
            env_file=None,
            state_dir=tmp_path / "state",
            dry_run=True,
            non_interactive=False,
        )
    )

    assert result == 0
    raw_env = captured["raw_env"]
    assert isinstance(raw_env, RawEnvInput)
    assert raw_env.values["DOKPLOY_SUBDOMAIN"] == "dokploy"
    assert raw_env.values["DOKPLOY_ADMIN_EMAIL"] == "clayton@superiorbyteworks.com"
    assert raw_env.values["AI_DEFAULT_API_KEY"] == "ai-key-123"
    assert raw_env.values["AI_DEFAULT_BASE_URL"] == "https://opencode.ai/zen/go/v1"
    assert raw_env.values["ENABLE_HEADSCALE"] == "true"
    assert "CLOUDFLARE_ZONE_ID" not in raw_env.values
    assert raw_env.values["PACKS"] == "openclaw"
    assert raw_env.values["OPENCLAW_CHANNELS"] == "telegram"
    assert raw_env.values["CLOUDFLARE_ACCESS_OTP_EMAILS"] == "clayton@superiorbyteworks.com"
    assert raw_env.values["OPENCLAW_GATEWAY_PASSWORD"] == "openclaw-ui-generated"
    assert "OPENCLAW_GATEWAY_TOKEN" not in raw_env.values


def test_apply_prompt_selection_preserves_existing_advisor_secrets_when_pack_stays_enabled() -> (
    None
):
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "PACKS": "openclaw",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_OPENROUTER_API_KEY": "or-key-existing",
            "OPENCLAW_NVIDIA_API_KEY": "nv-key-existing",
            "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
            "OPENCLAW_FALLBACK_MODELS": "openrouter/openrouter/free",
            "OPENCLAW_GATEWAY_TOKEN": "token-123",
            "OPENCLAW_GATEWAY_PASSWORD": "gateway-password-existing",
        },
    )

    updated = apply_prompt_selection(
        raw_env,
        PromptSelection(
            selected_packs=("openclaw",),
            disabled_packs=(),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={},
            advisor_env={},
            openclaw_channels=("telegram",),
            my_farm_advisor_channels=(),
        ),
    )

    assert updated.values["OPENCLAW_OPENROUTER_API_KEY"] == "or-key-existing"
    assert updated.values["OPENCLAW_NVIDIA_API_KEY"] == "nv-key-existing"
    assert updated.values["OPENCLAW_PRIMARY_MODEL"] == "nvidia/moonshotai/kimi-k2.5"
    assert updated.values["OPENCLAW_FALLBACK_MODELS"] == "openrouter/openrouter/free"
    assert updated.values["OPENCLAW_GATEWAY_TOKEN"] == "token-123"
    assert updated.values["OPENCLAW_GATEWAY_PASSWORD"] == "gateway-password-existing"


def test_apply_prompt_selection_removes_openclaw_secrets_when_pack_is_disabled() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "PACKS": "openclaw",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_OPENROUTER_API_KEY": "or-key-existing",
            "OPENCLAW_NVIDIA_API_KEY": "nv-key-existing",
            "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
            "OPENCLAW_FALLBACK_MODELS": "openrouter/openrouter/free",
            "OPENCLAW_GATEWAY_TOKEN": "token-123",
            "OPENCLAW_GATEWAY_PASSWORD": "gateway-password-existing",
        },
    )

    updated = apply_prompt_selection(
        raw_env,
        PromptSelection(
            selected_packs=(),
            disabled_packs=("openclaw",),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={},
            advisor_env={},
            openclaw_channels=(),
            my_farm_advisor_channels=(),
        ),
    )

    assert "OPENCLAW_CHANNELS" not in updated.values
    assert "OPENCLAW_OPENROUTER_API_KEY" not in updated.values
    assert "OPENCLAW_NVIDIA_API_KEY" not in updated.values
    assert "OPENCLAW_PRIMARY_MODEL" not in updated.values
    assert "OPENCLAW_FALLBACK_MODELS" not in updated.values
    assert "OPENCLAW_GATEWAY_TOKEN" not in updated.values
    assert "OPENCLAW_GATEWAY_PASSWORD" not in updated.values


def test_guided_install_prompt_collects_farm_runtime_values_with_shared_ai_defaults() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )
    responses = iter(
        [
            "n",  # matrix
            "y",  # nextcloud
            "y",  # seaweedfs
            "n",  # openclaw
            "y",  # my farm advisor
            "",  # farm channels default telegram
            "n",  # separate nvidia key uses shared default
            "n",  # separate fallback key uses shared default
            "",  # primary model optional
            "",  # fallback models default
            "farm-bot-token",
            "123456789",
            "anthropic-key",
            "https://integrate.api.nvidia.test",
            "field-token",
            "field-pair",
            "111,222",
            "pipeline-token",
            "pipeline-pair",
            "333,444",
            "555,666",
            "777,888",
            "owners-only",
            "America/Chicago",
            "1",
            "0",
            "n",  # skip optional R2/data settings
        ]
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
        shared_ai_default_configured=True,
    )
    monkeypatch.undo()

    assert selection.selected_packs == ("my-farm-advisor", "nextcloud", "seaweedfs")
    assert selection.openclaw_channels == ()
    assert selection.my_farm_advisor_channels == ("telegram",)
    assert selection.generated_secrets == {
        "MY_FARM_ADVISOR_GATEWAY_PASSWORD": "my-farm-ui-generated",
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }
    assert selection.advisor_env == {
        "ANTHROPIC_API_KEY": "anthropic-key",
        "MY_FARM_ADVISOR_FALLBACK_MODELS": "opencode-go/deepseek-v4-flash",
        "MY_FARM_ADVISOR_GATEWAY_PASSWORD": "my-farm-ui-generated",
        "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN": "farm-bot-token",
        "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID": "123456789",
        "NVIDIA_BASE_URL": "https://integrate.api.nvidia.test",
        "OPENCLAW_BOOTSTRAP_REFRESH": "1",
        "OPENCLAW_MEMORY_SEARCH_ENABLED": "0",
        "OPENCLAW_TELEGRAM_GROUP_POLICY": "owners-only",
        "TELEGRAM_ALLOWED_USERS": "777,888",
        "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS": "333,444",
        "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS": "555,666",
        "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE": "pipeline-pair",
        "TELEGRAM_DATA_PIPELINE_BOT_TOKEN": "pipeline-token",
        "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS": "111,222",
        "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE": "field-pair",
        "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN": "field-token",
        "TZ": "America/Chicago",
    }
    assert "MY_FARM_ADVISOR_NVIDIA_API_KEY" not in selection.advisor_env
    assert "MY_FARM_ADVISOR_OPENROUTER_API_KEY" not in selection.advisor_env


def test_guided_install_prompt_collects_openclaw_only_values() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )
    responses = iter(
        [
            "n",  # matrix
            "y",  # nextcloud
            "y",  # seaweedfs
            "y",  # openclaw
            "",  # openclaw channel default telegram
            "n",  # separate nvidia key uses shared default
            "n",  # separate fallback key uses shared default
            "",  # primary model optional
            "",  # fallback models default
            "openclaw-bot-token",
            "24680",
            "n",  # my farm advisor
        ]
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
        shared_ai_default_configured=True,
    )
    monkeypatch.undo()

    assert selection.selected_packs == ("nextcloud", "openclaw", "seaweedfs")
    assert selection.openclaw_channels == ("telegram",)
    assert selection.my_farm_advisor_channels == ()
    assert selection.generated_secrets == {
        "OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated",
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }
    assert selection.advisor_env == {
        "OPENCLAW_FALLBACK_MODELS": "opencode-go/deepseek-v4-flash",
        "OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated",
        "OPENCLAW_TELEGRAM_BOT_TOKEN": "openclaw-bot-token",
        "OPENCLAW_TELEGRAM_OWNER_USER_ID": "24680",
    }


def test_guided_install_prompt_keeps_farm_channels_independent_from_openclaw() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )
    responses = iter(
        [
            "n",  # matrix
            "y",  # nextcloud
            "y",  # seaweedfs
            "y",  # openclaw
            "",  # openclaw channel default telegram
            "n",  # separate nvidia key uses shared default
            "n",  # separate fallback key uses shared default
            "",  # openclaw primary model optional
            "",  # openclaw fallback models default
            "openclaw-bot-token",
            "24680",
            "y",  # my farm advisor
            "matrix",
            "n",  # farm separate nvidia key uses shared default
            "n",  # farm separate fallback key uses shared default
            "",  # farm primary model optional
            "",  # farm fallback models default
            "",  # anthropic
            "",  # nvidia base url
            "",  # field token
            "",  # field pairing
            "",  # field allowed
            "",  # pipeline token
            "",  # pipeline pairing
            "",  # pipeline allowed
            "",  # pipeline bot allowed
            "",  # global telegram allowed users
            "",  # group policy
            "",  # timezone
            "",  # bootstrap refresh
            "",  # memory search
            "n",  # skip optional R2/data settings
        ]
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
        shared_ai_default_configured=True,
    )
    monkeypatch.undo()

    assert selection.selected_packs == (
        "matrix",
        "my-farm-advisor",
        "nextcloud",
        "openclaw",
        "seaweedfs",
    )
    assert selection.openclaw_channels == ("telegram",)
    assert selection.my_farm_advisor_channels == ("matrix",)
    assert selection.advisor_env["OPENCLAW_TELEGRAM_BOT_TOKEN"] == "openclaw-bot-token"
    assert "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN" not in selection.advisor_env
    assert selection.advisor_env["MY_FARM_ADVISOR_FALLBACK_MODELS"] == "opencode-go/deepseek-v4-flash"


def test_apply_prompt_selection_disabling_farm_preserves_openclaw_values() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "PACKS": "my-farm-advisor,openclaw",
            "OPENCLAW_CHANNELS": "telegram",
            "MY_FARM_ADVISOR_CHANNELS": "telegram",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://opencode.ai/zen/go/v1",
            "OPENCLAW_OPENROUTER_API_KEY": "or-key-existing",
            "OPENCLAW_NVIDIA_API_KEY": "nv-key-existing",
            "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
            "OPENCLAW_FALLBACK_MODELS": "openrouter/openrouter/free",
            "OPENCLAW_GATEWAY_TOKEN": "token-123",
            "OPENCLAW_GATEWAY_PASSWORD": "openclaw-password-existing",
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-or-key",
            "MY_FARM_ADVISOR_NVIDIA_API_KEY": "farm-nv-key",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "openrouter/farm-primary",
            "MY_FARM_ADVISOR_FALLBACK_MODELS": "openrouter/farm-backup",
            "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN": "farm-bot-token",
            "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID": "123456789",
            "MY_FARM_ADVISOR_GATEWAY_PASSWORD": "farm-password-existing",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.test",
            "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN": "field-token",
            "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE": "field-pair",
            "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS": "111,222",
            "TELEGRAM_DATA_PIPELINE_BOT_TOKEN": "pipeline-token",
            "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE": "pipeline-pair",
            "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS": "333,444",
            "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS": "555,666",
            "TELEGRAM_ALLOWED_USERS": "777,888",
            "OPENCLAW_TELEGRAM_GROUP_POLICY": "owners-only",
            "TZ": "America/Chicago",
            "OPENCLAW_SYNC_SKILLS_ON_START": "1",
            "OPENCLAW_SYNC_SKILLS_OVERWRITE": "0",
            "OPENCLAW_FORCE_SKILL_SYNC": "0",
            "OPENCLAW_BOOTSTRAP_REFRESH": "1",
            "OPENCLAW_MEMORY_SEARCH_ENABLED": "0",
            "R2_BUCKET_NAME": "farm-bucket",
            "R2_ENDPOINT": "https://r2.example.com",
            "R2_ACCESS_KEY_ID": "farm-access",
            "R2_SECRET_ACCESS_KEY": "farm-secret",
            "CF_ACCOUNT_ID": "farm-account",
            "DATA_MODE": "r2",
            "WORKSPACE_DATA_R2_RCLONE_MOUNT": "1",
            "WORKSPACE_DATA_R2_PREFIX": "farm-prefix",
        },
    )

    updated = apply_prompt_selection(
        raw_env,
        PromptSelection(
            selected_packs=("openclaw",),
            disabled_packs=("my-farm-advisor",),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={},
            advisor_env={},
            openclaw_channels=("telegram",),
            my_farm_advisor_channels=(),
        ),
    )

    assert updated.values["OPENCLAW_CHANNELS"] == "telegram"
    assert updated.values["OPENCLAW_OPENROUTER_API_KEY"] == "or-key-existing"
    assert updated.values["OPENCLAW_NVIDIA_API_KEY"] == "nv-key-existing"
    assert updated.values["OPENCLAW_PRIMARY_MODEL"] == "nvidia/moonshotai/kimi-k2.5"
    assert updated.values["OPENCLAW_FALLBACK_MODELS"] == "openrouter/openrouter/free"
    assert updated.values["OPENCLAW_GATEWAY_TOKEN"] == "token-123"
    assert updated.values["OPENCLAW_GATEWAY_PASSWORD"] == "openclaw-password-existing"
    assert updated.values["AI_DEFAULT_API_KEY"] == "shared-key"
    assert updated.values["AI_DEFAULT_BASE_URL"] == "https://opencode.ai/zen/go/v1"
    assert updated.values["OPENCLAW_SYNC_SKILLS_ON_START"] == "1"
    assert updated.values["OPENCLAW_SYNC_SKILLS_OVERWRITE"] == "0"
    assert updated.values["OPENCLAW_FORCE_SKILL_SYNC"] == "0"
    assert updated.values["OPENCLAW_BOOTSTRAP_REFRESH"] == "1"
    assert updated.values["OPENCLAW_MEMORY_SEARCH_ENABLED"] == "0"
    for removed_key in (
        "MY_FARM_ADVISOR_CHANNELS",
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
        "MY_FARM_ADVISOR_NVIDIA_API_KEY",
        "MY_FARM_ADVISOR_PRIMARY_MODEL",
        "MY_FARM_ADVISOR_FALLBACK_MODELS",
        "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
        "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
        "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
        "ANTHROPIC_API_KEY",
        "NVIDIA_BASE_URL",
        "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
        "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE",
        "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS",
        "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
        "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE",
        "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS",
        "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS",
        "TELEGRAM_ALLOWED_USERS",
        "R2_BUCKET_NAME",
        "R2_ENDPOINT",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "CF_ACCOUNT_ID",
        "DATA_MODE",
        "WORKSPACE_DATA_R2_RCLONE_MOUNT",
        "WORKSPACE_DATA_R2_PREFIX",
    ):
        assert removed_key not in updated.values
