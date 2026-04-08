from __future__ import annotations

import argparse
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from dokploy_wizard import cli
from dokploy_wizard.dokploy import DokployBootstrapAuthError, DokployBootstrapAuthResult
from dokploy_wizard.lifecycle import applicable_phases_for
from dokploy_wizard.packs import prompts as prompt_module
from dokploy_wizard.packs.prompts import (
    GuidedInstallValues,
    PromptSelection,
    prompt_for_initial_install_values,
)
from dokploy_wizard.preflight import (
    HostFacts,
    PreflightCheck,
    PreflightError,
    PreflightReport,
    derive_required_profile,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"
FIXTURES_DIR = REPO_ROOT / "fixtures"


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
    assert "sensitive install.env operator file" in result.stdout
    assert "--no-print-secrets" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--non-interactive" in result.stdout
    assert "--allow-memory-shortfall" in result.stdout
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
            "",
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
    assert "Cloudflare zone ID (optional; press Enter to look up from example.com)" in combined
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
        no_print_secrets=False,
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
            generated_secrets={},
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
        no_print_secrets=False,
    )

    assert cli._handle_install(args) == 0
    env_file = custom_state_dir / "install.env"
    assert env_file.exists()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    env_contents = env_file.read_text(encoding="utf-8")
    assert "STACK_NAME=guided-stack" in env_contents
    assert "ROOT_DOMAIN=example.com" in env_contents
    assert "DOKPLOY_SUBDOMAIN=dokploy" in env_contents
    assert "DOKPLOY_ADMIN_EMAIL=admin@example.com" in env_contents
    assert "DOKPLOY_ADMIN_PASSWORD=secret-123" in env_contents
    assert "ENABLE_HEADSCALE=true" in env_contents
    assert "CLOUDFLARE_API_TOKEN=token-123" in env_contents
    assert "CLOUDFLARE_ZONE_ID" not in env_contents
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
            "",
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
            "",
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
            "",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses), output=print)

    assert values.cloudflare_api_token == "cf-token"
    captured = capsys.readouterr()
    assert "https://dash.cloudflare.com/profile/api-tokens" in captured.out
    assert "Zone -> DNS -> Edit" in captured.out


def test_guided_install_generates_seaweedfs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(["n", "n", "y", "n", "n"])
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
    )

    assert selection.seaweedfs_access_key == "seaweed-generated"
    assert selection.seaweedfs_secret_key == "seaweed-secret-generated"
    assert selection.generated_secrets == {
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }


def test_guided_install_prints_generated_seaweedfs_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._emit_generated_secrets(
        {
            "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
        },
        Path("/tmp/install.env"),
    )

    captured = capsys.readouterr()
    assert "Generated credentials" in captured.out
    assert "SEAWEEDFS_ACCESS_KEY=seaweed-generated" in captured.out
    assert "SEAWEEDFS_SECRET_KEY=seaweed-secret-generated" in captured.out


def test_write_reusable_env_file_sets_owner_only_permissions(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    cli._write_reusable_env_file(
        env_file,
        RawEnvInput(format_version=1, values={"STACK_NAME": "example"}),
    )

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_load_install_raw_env_warns_on_broad_permissions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    raw_env = cli._load_install_raw_env(
        env_file,
        non_interactive=True,
        warn_on_broad_permissions=True,
    )

    captured = capsys.readouterr()
    assert raw_env.values["STACK_NAME"] == "example"
    assert "permissions are broader than owner-only" in captured.err
    assert "0600" in captured.err
    assert str(env_file) in captured.err


def test_load_install_raw_env_skips_warning_when_permissions_are_owner_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o600)

    cli._load_install_raw_env(
        env_file,
        non_interactive=True,
        warn_on_broad_permissions=True,
    )

    captured = capsys.readouterr()
    assert captured.err == ""


def test_handle_install_suppresses_generated_secret_output_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"

    monkeypatch.setattr(
        cli,
        "_resolve_install_input",
        lambda **_: (
            env_file,
            RawEnvInput(format_version=1, values={"STACK_NAME": "example"}),
            tmp_path / "state",
            {"SEAWEEDFS_SECRET_KEY": "generated-secret"},
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=None,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=False,
            no_print_secrets=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "Generated credentials" not in captured.out
    assert "generated-secret" not in captured.out


def test_handle_modify_warns_on_broad_env_file_permissions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_modify_flow", lambda **_: {"ok": True})

    result = cli._handle_modify(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "permissions are broader than owner-only" in captured.err


def test_handle_modify_dry_run_skips_env_file_permission_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_modify_flow", lambda **_: {"ok": True})

    result = cli._handle_modify(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=True,
            non_interactive=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""


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
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_handle_install_warns_on_broad_env_file_permissions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=True,
            no_print_secrets=False,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "permissions are broader than owner-only" in captured.err


def test_handle_install_dry_run_skips_env_file_permission_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=True,
            non_interactive=True,
            no_print_secrets=False,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""


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


def test_run_install_flow_persists_scaffold_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    order: list[str] = []

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    monkeypatch.setattr(
        cli,
        "collect_host_facts",
        lambda _: _host_facts(),
    )
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda *_, **__: PreflightReport(
            host_facts=_host_facts(),
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )

    def fake_persist_install_scaffold(
        state_dir: Path, scaffold_raw_env: RawEnvInput, scaffold_desired_state: object
    ) -> None:
        del state_dir, scaffold_desired_state
        order.append("persist")
        assert "DOKPLOY_API_KEY" not in scaffold_raw_env.values

    def fake_ensure_dokploy_api_auth(**kwargs: object) -> RawEnvInput:
        del kwargs
        order.append("ensure")
        raise RuntimeError("stop after auth ordering check")

    monkeypatch.setattr(cli, "persist_install_scaffold", fake_persist_install_scaffold)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fake_ensure_dokploy_api_auth)

    with pytest.raises(RuntimeError, match="stop after auth ordering check"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=FakeBootstrapBackend(),
        )

    assert order == ["persist", "ensure"]


def test_install_rejects_mock_contamination_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "install.env"
    state_dir = tmp_path / "state"
    env_path.write_text(
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def fail_if_scaffold_called(*_: object, **__: object) -> None:
        raise AssertionError("persist_install_scaffold should not be reached")

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "persist_install_scaffold", fail_if_scaffold_called)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)

    with pytest.raises(SystemExit, match="live/pre-live runs require real integrations") as error:
        cli._handle_install(
            argparse.Namespace(
                env_file=env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
            )
        )

    message = str(error.value)
    assert not state_dir.exists()
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in message
    assert "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID" in message
    assert "DOKPLOY_BOOTSTRAP_HEALTHY" in message
    assert "DOKPLOY_BOOTSTRAP_MOCK_API_KEY" in message
    assert "DOKPLOY_MOCK_API_MODE" in message
    assert "HEADSCALE_MOCK_HEALTHY" in message


def test_install_bootstraps_missing_docker_before_strict_preflight_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    remediated_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    collected: list[HostFacts] = []
    remediation_calls: list[dict[str, object]] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, remediated_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda *, assessment, backend: remediation_calls.append(
            {
                "backend": backend,
                "missing_packages": assessment.missing_packages,
                "outcome": assessment.outcome,
            }
        ),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        collected.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "run_preflight", record_preflight)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collected == [remediated_host]
    assert remediation_calls == [
        {
            "backend": remediation_calls[0]["backend"],
            "missing_packages": ("docker.io",),
            "outcome": "missing_prerequisites",
        }
    ]
    assert summary["host_prerequisites"] == {
        "assessment": {
            "checks": [
                {
                    "detail": "Ubuntu 24.04 host detected for apt-backed prerequisite checks.",
                    "name": "os_support",
                    "package_name": None,
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'git' is installed.",
                    "name": "git",
                    "package_name": "git",
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'curl' is installed.",
                    "name": "curl",
                    "package_name": "curl",
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'ca-certificates' is installed.",
                    "name": "ca_certificates",
                    "package_name": "ca-certificates",
                    "status": "pass",
                },
                {
                    "detail": "required Ubuntu package 'docker.io' is not installed",
                    "name": "docker_io",
                    "package_name": "docker.io",
                    "status": "fail",
                },
                {
                    "detail": "Docker daemon is unavailable or unreachable",
                    "name": "docker_daemon",
                    "package_name": "docker.io",
                    "status": "fail",
                },
            ],
            "install_command": "sudo apt-get update && sudo apt-get install -y docker.io",
            "missing_packages": ["docker.io"],
            "notes": [
                "Missing apt-managed baseline packages can be remediated on this host.",
                "Docker daemon reachability is required before install can proceed.",
            ],
            "outcome": "missing_prerequisites",
            "remediation_eligible": True,
        },
        "post_remediation_host_facts": remediated_host.to_dict(),
        "remediation_actions": [
            {"action": "apt_install", "packages": ["docker.io"]},
            {"action": "ensure_docker_daemon"},
        ],
        "remediation_attempted": True,
    }


def test_install_on_unsupported_host_refuses_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    unsupported_host = _host_facts(distribution_id="debian", version_id="12")

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: unsupported_host)
    monkeypatch.setattr(
        cli,
        "UbuntuAptHostPrerequisiteBackend",
        lambda _: pytest.fail("unsupported host should fail before backend construction"),
    )
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("unsupported host should not attempt remediation"),
    )

    with pytest.raises(PreflightError, match="unsupported host OS 'debian 12'"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_install_attempts_docker_service_readiness_before_strict_preflight_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=True, docker_daemon_reachable=False)
    remediated_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    collected: list[HostFacts] = []
    remediation_calls: list[tuple[str, ...]] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            del package_name
            return True

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, remediated_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda *, assessment, backend: remediation_calls.append(assessment.missing_packages),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        collected.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "run_preflight", record_preflight)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collected == [remediated_host]
    assert remediation_calls == [()]
    assert summary["host_prerequisites"]["remediation_actions"] == [
        {"action": "ensure_docker_daemon"}
    ]


def test_install_leaves_supported_host_prerequisites_as_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    ready_host = _host_facts()
    collect_calls: list[RawEnvInput] = []
    preflight_hosts: list[HostFacts] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            del package_name
            return True

        def docker_daemon_reachable(self) -> bool:
            return True

    def collect_ready_host(raw: RawEnvInput) -> HostFacts:
        collect_calls.append(raw)
        return ready_host

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        preflight_hosts.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "collect_host_facts", collect_ready_host)
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("satisfied host prerequisites should not trigger remediation"),
    )
    monkeypatch.setattr(cli, "run_preflight", record_preflight)
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collect_calls == [raw_env]
    assert preflight_hosts == [ready_host]
    assert summary["host_prerequisites"] == {
        "assessment": {
            "checks": [
                {
                    "detail": "Ubuntu 24.04 host detected for apt-backed prerequisite checks.",
                    "name": "os_support",
                    "package_name": None,
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'git' is installed.",
                    "name": "git",
                    "package_name": "git",
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'curl' is installed.",
                    "name": "curl",
                    "package_name": "curl",
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'ca-certificates' is installed.",
                    "name": "ca_certificates",
                    "package_name": "ca-certificates",
                    "status": "pass",
                },
                {
                    "detail": "Ubuntu package 'docker.io' is installed.",
                    "name": "docker_io",
                    "package_name": "docker.io",
                    "status": "pass",
                },
                {
                    "detail": "Docker daemon responded successfully.",
                    "name": "docker_daemon",
                    "package_name": "docker.io",
                    "status": "pass",
                },
            ],
            "install_command": None,
            "missing_packages": [],
            "notes": ["Baseline Ubuntu 24.04 host prerequisites are already satisfied."],
            "outcome": "noop",
            "remediation_eligible": True,
        },
        "remediation_actions": [],
        "remediation_attempted": False,
    }


def test_install_prompts_before_continuing_on_memory_only_shortfall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr("builtins.input", fake_input)
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        prompt_for_memory_shortfall=True,
    )

    assert prompts == ["Proceed anyway? [y/N] "]
    assert summary["ok"] is True


def test_install_allows_non_interactive_memory_shortfall_with_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(
            f"unexpected prompt with explicit memory override flag: {prompt}"
        ),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        allow_memory_shortfall=True,
    )

    assert summary["ok"] is True


def test_install_requires_allow_memory_shortfall_flag_for_non_interactive_memory_only_shortfall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(
        PreflightError,
        match="Rerun install with --allow-memory-shortfall to continue non-interactively",
    ):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_install_does_not_allow_cpu_shortfall_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(cpu_count=1, memory_gb=16))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(f"unexpected prompt for hard-stop preflight failure: {prompt}"),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(PreflightError, match="insufficient CPU for Core: need 2 vCPU, found 1"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
            allow_memory_shortfall=True,
            prompt_for_memory_shortfall=True,
        )


def test_install_does_not_allow_disk_shortfall_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(disk_gb=20, memory_gb=16))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(f"unexpected prompt for hard-stop preflight failure: {prompt}"),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(PreflightError, match="insufficient disk for Core: need 40 GB, found 20 GB"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
            allow_memory_shortfall=True,
            prompt_for_memory_shortfall=True,
        )


def test_install_reports_explicit_rerun_with_sudo_guidance_on_apt_privilege_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: initial_host)
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: (_ for _ in ()).throw(
            StateValidationError(
                "Baseline host prerequisite remediation requires apt/systemd privileges; "
                "rerun dokploy-wizard install as root or with sudo."
            )
        ),
    )

    with pytest.raises(
        StateValidationError,
        match="rerun dokploy-wizard install as root or with sudo",
    ):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_modify_rejects_mock_contamination_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    modify_env_path = tmp_path / "modify.env"
    modify_env_path.write_text(
        (FIXTURES_DIR / "lifecycle-headscale.env")
        .read_text(encoding="utf-8")
        .replace("ROOT_DOMAIN=example.com", "ROOT_DOMAIN=example.net"),
        encoding="utf-8",
    )

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)

    with pytest.raises(SystemExit, match="live/pre-live runs require real integrations") as error:
        cli._handle_modify(
            argparse.Namespace(
                env_file=modify_env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
            )
        )

    message = str(error.value)
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in message
    assert "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID" in message
    assert "DOKPLOY_BOOTSTRAP_HEALTHY" in message
    assert "DOKPLOY_BOOTSTRAP_MOCK_API_KEY" in message
    assert "DOKPLOY_MOCK_API_MODE" in message
    assert "HEADSCALE_MOCK_HEALTHY" in message


def test_modify_does_not_gain_host_prerequisite_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )

    missing_docker_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: missing_docker_host)
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("modify should not attempt host prerequisite remediation"),
    )

    with pytest.raises(PreflightError, match="Docker is not installed"):
        cli.run_modify_flow(
            env_file=tmp_path / "modify.env",
            state_dir=state_dir,
            dry_run=False,
            raw_env=existing_raw,
            bootstrap_backend=_FakeBootstrapBackend(),
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


class _FakeBootstrapBackend:
    def is_healthy(self) -> bool:
        return True

    def install(self) -> None:
        raise AssertionError("install should not be called")


def _stub_install_flow_after_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})


def _host_facts(
    *,
    distribution_id: str = "ubuntu",
    version_id: str = "24.04",
    cpu_count: int = 8,
    memory_gb: int = 16,
    disk_gb: int = 200,
    docker_installed: bool = True,
    docker_daemon_reachable: bool = True,
) -> HostFacts:
    return HostFacts(
        distribution_id=distribution_id,
        version_id=version_id,
        cpu_count=cpu_count,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
        disk_path="/var/lib/docker",
        docker_installed=docker_installed,
        docker_daemon_reachable=docker_daemon_reachable,
        ports_in_use=(),
        environment_classification="vps",
        hostname="test-host",
    )
