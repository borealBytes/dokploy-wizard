# pyright: reportMissingImports=false

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.packs.prompts import GuidedInstallValues, PromptSelection
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
    assert desired_state["enabled_packs"] == ["headscale", "openclaw"]
    assert desired_state["hostnames"]["openclaw"] == "openclaw.example.com"
    assert desired_state["openclaw_channels"] == ["telegram"]
    assert payload["preflight"]["required_profile"]["name"] == "Recommended"


def test_both_advisor_packs_can_be_selected_together(tmp_path: Path) -> None:
    result = _run_cli(
        "install",
        "--env-file",
        str(FIXTURES_DIR / "invalid-pack-combo.env"),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    desired_state = payload["desired_state"]
    assert desired_state["enabled_packs"] == [
        "headscale",
        "my-farm-advisor",
        "openclaw",
    ]


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
    assert raw_env.values["ENABLE_HEADSCALE"] == "true"
    assert raw_env.values["PACKS"] == "openclaw"
    assert raw_env.values["OPENCLAW_CHANNELS"] == "telegram"
