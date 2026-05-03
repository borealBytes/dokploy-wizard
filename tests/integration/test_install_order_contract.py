# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.state import RawEnvInput, parse_env_file
from dokploy_wizard.tailscale import ShellTailscaleBackend

from tests.integration.test_networking_reconciler import FakeCoderBackend
from tests.integration.test_nextcloud_pack import (
    FakeCloudflareBackend,
    FakeDokployBackend,
    FakeNextcloudBackend,
    FakeOpenClawBackend,
    FakeSeaweedFsBackend,
    FakeSharedCoreBackend,
)
from tests.unit.test_tailscale_phase import FakeRunner


_CORE_ONLY_STRIPPED_PREFIXES = (
    "ENABLE_",
    "DOCUSEAL_",
    "HEADSCALE_",
    "OPENCLAW_",
    "MY_FARM_ADVISOR_",
    "MATRIX_",
    "MOODLE_",
    "NEXTCLOUD_",
    "ONLYOFFICE_",
    "SEAWEEDFS_",
    "CODER_",
    "HERMES_",
    "TELEGRAM_",
    "LITELLM_",
    "R2_",
    "WORKSPACE_",
)
_CORE_ONLY_STRIPPED_KEYS = {
    "ADVISOR_GATEWAY_PASSWORD",
    "CLOUDFLARE_ACCESS_OTP_EMAILS",
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "ANTHROPIC_API_KEY",
    "CF_ACCOUNT_ID",
    "DATA_MODE",
    "NVIDIA_BASE_URL",
    "OPENCLAW_MEMORY_SEARCH_ENABLED",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_env_with_host_facts(*, packs: str, strip_pack_env: bool = False) -> RawEnvInput:
    raw_env = parse_env_file(_repo_root() / ".install.env")
    values = {
        key: value
        for key, value in raw_env.values.items()
        if key != "ENABLE_TAILSCALE" and not key.startswith("TAILSCALE_")
    }
    if strip_pack_env:
        values = {
            key: value
            for key, value in values.items()
            if key not in _CORE_ONLY_STRIPPED_KEYS
            and not key.startswith(_CORE_ONLY_STRIPPED_PREFIXES)
        }
    return RawEnvInput(
        format_version=raw_env.format_version,
        values={
            **values,
            "PACKS": packs,
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


def _load_mvp_env_with_host_facts() -> RawEnvInput:
    return _load_env_with_host_facts(packs="nextcloud,openclaw,seaweedfs,coder")


def test_root_mvp_env_emits_current_install_order_contract(tmp_path: Path) -> None:
    summary = run_install_flow(
        env_file=_repo_root() / ".install.env",
        state_dir=tmp_path / "state",
        dry_run=True,
        raw_env=_load_mvp_env_with_host_facts(),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        tailscale_backend=ShellTailscaleBackend(_load_mvp_env_with_host_facts(), runner=FakeRunner()),
        nextcloud_backend=FakeNextcloudBackend(),
        seaweedfs_backend=FakeSeaweedFsBackend(),
        coder_backend=FakeCoderBackend(),
        openclaw_backend=FakeOpenClawBackend(),
    )

    assert summary["desired_state"]["selected_packs"] == [
        "coder",
        "my-farm-advisor",
        "nextcloud",
        "openclaw",
        "seaweedfs",
    ]
    assert summary["desired_state"]["enabled_packs"] == [
        "coder",
        "my-farm-advisor",
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
        "my-farm-advisor",
        "cloudflare_access",
    ]
    assert summary["lifecycle"]["phases_to_run"] == summary["lifecycle"]["applicable_phases"][1:]
    assert "coder" in summary["lifecycle"]["applicable_phases"]
    assert "headscale" not in summary["lifecycle"]["applicable_phases"]
    assert summary["desired_state"]["shared_core"]["litellm"]["service_name"].endswith(
        "-shared-litellm"
    )


def test_litellm_phase_precedes_ai_consumers(tmp_path: Path) -> None:
    ai_summary = run_install_flow(
        env_file=_repo_root() / ".install.env",
        state_dir=tmp_path / "state-ai",
        dry_run=True,
        raw_env=_load_env_with_host_facts(
            packs="nextcloud,openclaw,seaweedfs,coder,my-farm-advisor"
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        tailscale_backend=ShellTailscaleBackend(
            _load_env_with_host_facts(packs="nextcloud,openclaw,seaweedfs,coder,my-farm-advisor"),
            runner=FakeRunner(),
        ),
        nextcloud_backend=FakeNextcloudBackend(),
        seaweedfs_backend=FakeSeaweedFsBackend(),
        coder_backend=FakeCoderBackend(),
        openclaw_backend=FakeOpenClawBackend(),
    )
    core_only_summary = run_install_flow(
        env_file=_repo_root() / ".install.env",
        state_dir=tmp_path / "state-core-only",
        dry_run=True,
        raw_env=_load_env_with_host_facts(packs="", strip_pack_env=True),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        tailscale_backend=ShellTailscaleBackend(
            _load_env_with_host_facts(packs="", strip_pack_env=True),
            runner=FakeRunner(),
        ),
        nextcloud_backend=FakeNextcloudBackend(),
        seaweedfs_backend=FakeSeaweedFsBackend(),
        coder_backend=FakeCoderBackend(),
        openclaw_backend=FakeOpenClawBackend(),
    )

    applicable_phases = ai_summary["lifecycle"]["applicable_phases"]
    assert applicable_phases.index("shared_core") < applicable_phases.index("coder")
    assert applicable_phases.index("shared_core") < applicable_phases.index("openclaw")
    assert applicable_phases.index("shared_core") < applicable_phases.index("my-farm-advisor")
    assert ai_summary["desired_state"]["shared_core"]["litellm"]["service_name"].endswith(
        "-shared-litellm"
    )
    assert core_only_summary["lifecycle"]["applicable_phases"] == [
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    ]
    assert core_only_summary["desired_state"]["shared_core"]["litellm"]["service_name"].endswith(
        "-shared-litellm"
    )
