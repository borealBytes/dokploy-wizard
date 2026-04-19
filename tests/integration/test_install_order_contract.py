# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.state import RawEnvInput, parse_env_file

from tests.integration.test_nextcloud_pack import FakeCloudflareBackend, FakeDokployBackend


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_mvp_env_with_host_facts() -> RawEnvInput:
    raw_env = parse_env_file(_repo_root() / ".install.env")
    return RawEnvInput(
        format_version=raw_env.format_version,
        values={
            **raw_env.values,
            "HOST_OS_ID": "ubuntu",
            "HOST_OS_VERSION_ID": "24.04",
            "HOST_CPU_COUNT": "6",
            "HOST_MEMORY_GB": "12",
            "HOST_DISK_GB": "150",
            "HOST_DOCKER_INSTALLED": "true",
            "HOST_DOCKER_DAEMON_REACHABLE": "true",
            "HOST_PORT_80_IN_USE": "false",
            "HOST_PORT_443_IN_USE": "false",
            "HOST_PORT_3000_IN_USE": "false",
            "HOST_ENVIRONMENT": "local",
        },
    )


def test_root_mvp_env_emits_current_install_order_contract(tmp_path: Path) -> None:
    summary = run_install_flow(
        env_file=_repo_root() / ".install.env",
        state_dir=tmp_path / "state",
        dry_run=True,
        raw_env=_load_mvp_env_with_host_facts(),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
    )

    assert summary["desired_state"]["selected_packs"] == [
        "coder",
        "nextcloud",
        "openclaw",
        "seaweedfs",
    ]
    assert summary["desired_state"]["enabled_packs"] == [
        "coder",
        "nextcloud",
        "openclaw",
        "seaweedfs",
    ]
    assert summary["lifecycle"]["applicable_phases"] == [
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "seaweedfs",
        "nextcloud",
        "coder",
        "openclaw",
        "cloudflare_access",
    ]
    assert summary["lifecycle"]["phases_to_run"] == summary["lifecycle"]["applicable_phases"][1:]
    assert "coder" in summary["lifecycle"]["applicable_phases"]
    assert "headscale" not in summary["lifecycle"]["applicable_phases"]
