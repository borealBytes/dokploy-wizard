# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

from dokploy_wizard.host_prereqs import (
    APT_LOCK_TIMEOUT_SECONDS,
    DOCKER_APT_PACKAGES,
    DOCKER_APT_REPOSITORY_URL,
    DOCKER_APT_SOURCE_PATH,
    _apt_get_command,
    _docker_apt_source_line,
    assess_host_prerequisites,
    remediate_host_prerequisites,
)
from dokploy_wizard.preflight import HostFacts


@dataclass
class FakeHostPrereqBackend:
    installed_packages: set[str] = field(default_factory=set)
    docker_daemon_ready: bool = False
    calls: list[tuple[str, object]] = field(default_factory=list)

    def package_installed(self, package_name: str) -> bool:
        return package_name in self.installed_packages

    def docker_daemon_reachable(self) -> bool:
        return self.docker_daemon_ready

    def install_packages(self, package_names: tuple[str, ...]) -> None:
        self.calls.append(("install_packages", package_names))

    def bootstrap_docker_engine(self) -> None:
        self.calls.append(("bootstrap_docker_engine", DOCKER_APT_PACKAGES))

    def ensure_docker_daemon(self) -> None:
        self.calls.append(("ensure_docker_daemon", "docker"))


def test_fresh_ubuntu_24_04_host_requires_official_docker_apt_bootstrap() -> None:
    host_facts = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    backend = FakeHostPrereqBackend(installed_packages={"git", "curl", "ca-certificates"})

    result = assess_host_prerequisites(host_facts=host_facts, backend=backend)

    assert result.outcome == "missing_prerequisites"
    assert result.remediation_eligible is True
    assert result.missing_packages == ()
    assert result.docker_bootstrap_required is True
    assert tuple((check.name, check.status) for check in result.checks) == (
        ("os_support", "pass"),
        ("git", "pass"),
        ("curl", "pass"),
        ("ca_certificates", "pass"),
        ("docker_cli", "fail"),
        ("docker_daemon", "fail"),
    )
    assert "official Ubuntu apt repository" in " ".join(result.notes)
    assert result.install_command is not None
    assert DOCKER_APT_REPOSITORY_URL in result.install_command
    for package_name in DOCKER_APT_PACKAGES:
        assert package_name in result.install_command
    assert "sudo systemctl enable --now docker" in result.install_command


def test_remediation_runs_baseline_packages_then_docker_bootstrap_then_daemon_enablement() -> None:
    host_facts = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    backend = FakeHostPrereqBackend(installed_packages={"git"})

    assessment = assess_host_prerequisites(host_facts=host_facts, backend=backend)
    remediate_host_prerequisites(assessment=assessment, backend=backend)

    assert assessment.missing_packages == ("curl", "ca-certificates")
    assert assessment.docker_bootstrap_required is True
    assert backend.calls == [
        ("install_packages", ("curl", "ca-certificates")),
        ("bootstrap_docker_engine", DOCKER_APT_PACKAGES),
        ("ensure_docker_daemon", "docker"),
    ]


def test_docker_apt_source_line_uses_concrete_architecture_token(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.host_prereqs._docker_apt_architecture",
        lambda: "arm64",
    )

    source_line = _docker_apt_source_line()

    assert source_line == (
        "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] "
        "https://download.docker.com/linux/ubuntu noble stable"
    )
    assert "$" not in source_line
    assert "$(dpkg --print-architecture)" not in source_line
    install_command = assess_host_prerequisites(
        host_facts=_host_facts(docker_installed=False, docker_daemon_reachable=False),
        backend=FakeHostPrereqBackend(installed_packages={"git", "curl", "ca-certificates"}),
    )
    assert install_command.install_command is not None
    assert f"sudo tee {DOCKER_APT_SOURCE_PATH}" in install_command.install_command


def test_apt_get_command_uses_dpkg_lock_timeout() -> None:
    assert _apt_get_command("update") == (
        "apt-get",
        "-o",
        f"DPkg::Lock::Timeout={APT_LOCK_TIMEOUT_SECONDS}",
        "update",
    )
    assert _apt_get_command("install", "-y", "docker-ce") == (
        "apt-get",
        "-o",
        f"DPkg::Lock::Timeout={APT_LOCK_TIMEOUT_SECONDS}",
        "install",
        "-y",
        "docker-ce",
    )


def _host_facts(*, docker_installed: bool, docker_daemon_reachable: bool) -> HostFacts:
    return HostFacts(
        distribution_id="ubuntu",
        version_id="24.04",
        cpu_count=4,
        memory_gb=8,
        disk_gb=100,
        disk_path="/var/lib/docker",
        docker_installed=docker_installed,
        docker_daemon_reachable=docker_daemon_reachable,
        ports_in_use=(),
        environment_classification="cloud",
        hostname="fresh-vps",
    )
