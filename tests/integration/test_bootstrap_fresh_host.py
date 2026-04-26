# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from dokploy_wizard import cli
from dokploy_wizard.preflight import (
    HostFacts,
    PreflightCheck,
    PreflightReport,
    derive_required_profile,
)
from dokploy_wizard.state import StateValidationError, parse_env_file, resolve_desired_state

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


class _FakeBootstrapBackend:
    def is_healthy(self) -> bool:
        return True

    def install(self) -> None:
        raise AssertionError("install should not be called in this test")


def test_fresh_ubuntu_install_bootstraps_docker_before_dokploy_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    remediated_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    host_fact_sequence = iter((initial_host, remediated_host))

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name in {"git", "curl", "ca-certificates"}

        def docker_daemon_reachable(self) -> bool:
            return False

        def install_packages(self, package_names: tuple[str, ...]) -> None:
            del package_names

        def bootstrap_docker_engine(self) -> None:
            return None

        def ensure_docker_daemon(self) -> None:
            return None

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["host_prerequisites"]["assessment"]["docker_bootstrap_required"] is True
    assert summary["host_prerequisites"]["assessment"]["missing_packages"] == []
    assert summary["host_prerequisites"]["remediation_actions"] == [
        {
            "action": "bootstrap_docker_engine",
            "packages": [
                "docker-ce",
                "docker-ce-cli",
                "containerd.io",
                "docker-buildx-plugin",
                "docker-compose-plugin",
            ],
            "repository": "official_docker_apt_repository",
        },
        {"action": "ensure_docker_daemon"},
    ]
    assert summary["host_prerequisites"]["post_remediation_host_facts"] == remediated_host.to_dict()


def test_fresh_ubuntu_install_reports_actionable_privilege_failure_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name in {"git", "curl", "ca-certificates"}

        def docker_daemon_reachable(self) -> bool:
            return False

        def install_packages(self, package_names: tuple[str, ...]) -> None:
            del package_names

        def bootstrap_docker_engine(self) -> None:
            return None

        def ensure_docker_daemon(self) -> None:
            return None

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
    monkeypatch.setattr(cli, "_build_coder_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_openclaw_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})


def _host_facts(*, docker_installed: bool, docker_daemon_reachable: bool) -> HostFacts:
    return HostFacts(
        distribution_id="ubuntu",
        version_id="24.04",
        cpu_count=8,
        memory_gb=16,
        disk_gb=200,
        disk_path="/var/lib/docker",
        docker_installed=docker_installed,
        docker_daemon_reachable=docker_daemon_reachable,
        ports_in_use=(),
        environment_classification="cloud",
        hostname="fresh-vps",
    )
