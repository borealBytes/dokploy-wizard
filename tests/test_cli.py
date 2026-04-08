from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.dokploy import DokployBootstrapAuthError, DokployBootstrapAuthResult
from dokploy_wizard.packs.prompts import (
    GuidedInstallValues,
    PromptSelection,
    prompt_for_initial_install_values,
)
from dokploy_wizard.state import RawEnvInput, resolve_desired_state

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_help_lists_expected_subcommands() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "inspect-state" in result.stdout
    assert "install" in result.stdout
    assert "modify" in result.stdout
    assert "uninstall" in result.stdout
    assert result.stderr == ""


def test_inspect_state_help_lists_task_two_flags() -> None:
    result = run_cli("inspect-state", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert result.stderr == ""


def test_install_help_lists_task_three_flags() -> None:
    result = run_cli("install", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "guided first-run install" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--non-interactive" in result.stdout
    assert result.stderr == ""


def test_guided_install_prompts_include_dokploy_guidance() -> None:
    prompts: list[str] = []
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "n",
            "n",
            "cf-token",
            "account-123",
            "zone-123",
        ]
    )

    def fake_prompt(message: str) -> str:
        prompts.append(message)
        return next(responses)

    values = prompt_for_initial_install_values(fake_prompt)

    assert values.stack_name == "example"
    assert values.dokploy_subdomain == "dokploy"
    assert values.dokploy_admin_email == "admin@example.com"
    assert values.dokploy_admin_password == "secret-123"
    assert values.enable_headscale is True
    assert values.enable_tailscale is False
    combined = "\n".join(prompts)
    assert "Dokploy subdomain" in combined
    assert "create the first admin and mint an API key" in combined
    assert "Private network mode" in combined
    assert "Need help finding your Cloudflare token" in combined
    assert "Tailscale auth key" not in combined


def test_install_parser_allows_missing_env_file() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(["install"])

    assert args.env_file is None


def test_install_without_env_file_fails_cleanly_in_non_interactive_mode(tmp_path: Path) -> None:
    args = argparse.Namespace(
        env_file=None,
        state_dir=tmp_path / "state",
        dry_run=False,
        non_interactive=True,
    )

    with pytest.raises(SystemExit, match="--env-file is required when --non-interactive"):
        cli._handle_install(args)


def test_guided_install_writes_env_file_and_runs_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    custom_state_dir = tmp_path / "custom-state"

    def fake_run_install_flow(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_for_guided_state_dir", lambda _: custom_state_dir)
    monkeypatch.setattr(
        cli,
        "prompt_for_initial_install_values",
        lambda **kwargs: GuidedInstallValues(
            stack_name="guided-stack",
            root_domain="example.com",
            dokploy_subdomain="dokploy",
            dokploy_admin_email="admin@example.com",
            dokploy_admin_password="secret-123",
            enable_headscale=True,
            cloudflare_api_token="token-123",
            cloudflare_account_id="account-123",
            cloudflare_zone_id="zone-123",
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
            openclaw_channels=("telegram",),
            my_farm_advisor_channels=(),
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", fake_run_install_flow)

    args = argparse.Namespace(
        env_file=None,
        state_dir=tmp_path / "state",
        dry_run=False,
        non_interactive=False,
    )

    assert cli._handle_install(args) == 0
    env_file = custom_state_dir / "install.env"
    assert env_file.exists()
    env_contents = env_file.read_text(encoding="utf-8")
    assert "STACK_NAME=guided-stack" in env_contents
    assert "ROOT_DOMAIN=example.com" in env_contents
    assert "DOKPLOY_SUBDOMAIN=dokploy" in env_contents
    assert "DOKPLOY_ADMIN_EMAIL=admin@example.com" in env_contents
    assert "DOKPLOY_ADMIN_PASSWORD=secret-123" in env_contents
    assert "ENABLE_HEADSCALE=true" in env_contents
    assert "CLOUDFLARE_API_TOKEN=token-123" in env_contents
    assert "PACKS=openclaw" in env_contents
    assert "OPENCLAW_CHANNELS=telegram" in env_contents
    assert captured["env_file"] == env_file
    assert captured["state_dir"] == custom_state_dir
    assert captured["dry_run"] is False
    raw_env = captured["raw_env"]
    assert isinstance(raw_env, RawEnvInput)
    assert raw_env.values["STACK_NAME"] == "guided-stack"
    assert raw_env.values["DOKPLOY_SUBDOMAIN"] == "dokploy"


def test_guided_dry_run_does_not_require_dokploy_admin_password() -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "",
            "n",
            "cf-token",
            "account-123",
            "zone-123",
        ]
    )

    values = prompt_for_initial_install_values(
        lambda _: next(responses), require_dokploy_auth=False
    )

    assert values.dokploy_admin_password is None
    assert values.enable_headscale is True


def test_guided_install_tailscale_mode_prompts_for_auth_key() -> None:
    prompts: list[str] = []
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "tailscale",
            "tskey-123",
            "wizard-host",
            "y",
            "tag:admin",
            "10.254.0.0/24",
            "n",
            "cf-token",
            "account-123",
            "zone-123",
        ]
    )

    def fake_prompt(message: str) -> str:
        prompts.append(message)
        return next(responses)

    values = prompt_for_initial_install_values(fake_prompt)

    assert values.stack_name == "example"
    assert values.enable_headscale is False
    assert values.enable_tailscale is True
    assert values.tailscale_auth_key == "tskey-123"
    assert values.tailscale_hostname == "wizard-host"
    assert values.tailscale_enable_ssh is True
    assert values.tailscale_tags == ("tag:admin",)
    assert values.tailscale_subnet_routes == ("10.254.0.0/24",)
    combined = "\n".join(prompts)
    assert "Tailscale auth key" in combined


def test_guided_install_can_emit_cloudflare_help(capsys: pytest.CaptureFixture[str]) -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "y",
            "cf-token",
            "account-123",
            "zone-123",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses), output=print)

    assert values.cloudflare_api_token == "cf-token"
    captured = capsys.readouterr()
    assert "https://dash.cloudflare.com/profile/api-tokens" in captured.out
    assert "DNS Write" in captured.out


def test_ensure_dokploy_api_auth_rewrites_env_with_generated_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            assert admin_email == "admin@example.com"
            assert admin_password == "secret-123"
            return DokployBootstrapAuthResult(
                api_key="dokp-key-123",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "dokp-key-123"
    assert updated.values["DOKPLOY_API_URL"] == "https://dokploy.example.com"
    written = env_file.read_text(encoding="utf-8")
    assert "DOKPLOY_API_KEY=dokp-key-123" in written


def test_ensure_dokploy_api_auth_fails_when_auth_cannot_be_bootstrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            del base_url

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            raise DokployBootstrapAuthError("no working auth endpoint")

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)

    with pytest.raises(DokployBootstrapAuthError, match="no working auth endpoint"):
        cli._ensure_dokploy_api_auth(
            env_file=env_file,
            raw_env=raw_env,
            desired_state=desired_state,
            bootstrap_backend=FakeBootstrapBackend(),
            dry_run=False,
            require_real_dokploy_auth=True,
        )


def test_modify_help_lists_task_eleven_flags() -> None:
    result = run_cli("modify", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--non-interactive" in result.stdout
    assert result.stderr == ""


def test_uninstall_help_lists_task_twelve_flags() -> None:
    result = run_cli("uninstall", "--help")

    assert result.returncode == 0
    assert "--retain-data" in result.stdout
    assert "--destroy-data" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--confirm-file" in result.stdout
    assert "--non-interactive" in result.stdout
    assert result.stderr == ""


def test_invalid_subcommand_fails_cleanly() -> None:
    result = run_cli("unknown-command")

    assert result.returncode != 0
    combined_output = f"{result.stdout}{result.stderr}"
    assert "usage:" in combined_output
    assert "invalid choice" in combined_output
    assert "unknown-command" in combined_output
