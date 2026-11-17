# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from dokploy_wizard import cli
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


def test_env_driven_selection_flow_resolves_requested_packs_from_flags(tmp_path: Path) -> None:
    result = _run_cli(
        "install",
        "--env-file",
        str(FIXTURES_DIR / "matrix.env"),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    desired_state = payload["desired_state"]
    assert desired_state["selected_packs"] == ["headscale", "matrix"]
    assert desired_state["enabled_packs"] == ["headscale", "matrix"]
    assert desired_state["hostnames"]["matrix"] == "matrix.example.com"
    assert desired_state["hostnames"]["headscale"] == "headscale.example.com"
    assert payload["preflight"]["required_profile"]["name"] == "Recommended"


def test_cli_install_rejects_unknown_pack_selection(tmp_path: Path) -> None:
    # fixtures/invalid-pack-combo.env selects a retired pack name, which is now a
    # plain unknown selection, so the CLI must fail with the unknown-pack error.
    result = _run_cli(
        "install",
        "--env-file",
        str(FIXTURES_DIR / "invalid-pack-combo.env"),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode != 0
    assert "Unknown pack selection(s) in PACKS: ['openclaw']." in result.stderr


def test_several_packs_can_be_selected_together(tmp_path: Path) -> None:
    env_file = tmp_path / "multi-pack.env"
    env_file.write_text(
        "\n".join(
            (
                "STACK_NAME=multi-pack-stack",
                "ROOT_DOMAIN=example.com",
                "PACKS=moodle,docuseal,matrix,coder",
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
                "CLOUDFLARE_TUNNEL_NAME=multi-pack-stack-tunnel",
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
    assert desired_state["enabled_packs"] == ["coder", "docuseal", "matrix", "moodle"]


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
            dokploy_admin_email="admin@example.com",
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
            selected_packs=("coder", "seaweedfs"),
            disabled_packs=(),
            seaweedfs_access_key="seaweed-generated",
            seaweedfs_secret_key="seaweed-secret-generated",
            generated_secrets={
                "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
            },
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
    assert raw_env.values["DOKPLOY_ADMIN_EMAIL"] == "admin@example.com"
    assert raw_env.values["AI_DEFAULT_API_KEY"] == "ai-key-123"
    assert raw_env.values["AI_DEFAULT_BASE_URL"] == "https://opencode.ai/zen/go/v1"
    assert raw_env.values["ENABLE_HEADSCALE"] == "true"
    assert "CLOUDFLARE_ZONE_ID" not in raw_env.values
    assert raw_env.values["PACKS"] == "coder,seaweedfs"
    assert raw_env.values["SEAWEEDFS_ACCESS_KEY"] == "seaweed-generated"
    assert raw_env.values["SEAWEEDFS_SECRET_KEY"] == "seaweed-secret-generated"


def test_apply_prompt_selection_marks_disabled_pack_with_env_flag_and_drops_credentials() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "PACKS": "nextcloud,coder",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access-existing",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret-existing",
        },
    )

    updated = apply_prompt_selection(
        raw_env,
        PromptSelection(
            selected_packs=(),
            disabled_packs=("nextcloud",),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={},
        ),
    )

    assert "PACKS" not in updated.values
    assert updated.values["ENABLE_NEXTCLOUD"] == "false"
    assert "SEAWEEDFS_ACCESS_KEY" not in updated.values
    assert "SEAWEEDFS_SECRET_KEY" not in updated.values


def test_apply_prompt_selection_records_enabled_packs_and_generated_credentials() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "ROOT_DOMAIN": "example.com",
        },
    )

    updated = apply_prompt_selection(
        raw_env,
        PromptSelection(
            selected_packs=("coder", "seaweedfs"),
            disabled_packs=("headscale",),
            seaweedfs_access_key="seaweed-generated",
            seaweedfs_secret_key="seaweed-secret-generated",
            generated_secrets={
                "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
            },
        ),
    )

    assert updated.values["PACKS"] == "coder,seaweedfs"
    assert updated.values["ENABLE_HEADSCALE"] == "false"
    assert updated.values["SEAWEEDFS_ACCESS_KEY"] == "seaweed-generated"
    assert updated.values["SEAWEEDFS_SECRET_KEY"] == "seaweed-secret-generated"
