"""Baseline host prerequisite detection for Dokploy wizard installs."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal, Protocol

from dokploy_wizard.preflight import (
    SUPPORTED_OS_ID,
    SUPPORTED_OS_VERSION,
    HostFacts,
    _is_supported_ubuntu_version,
)
from dokploy_wizard.state import RawEnvInput, StateValidationError

APT_INSTALL_PREFIX = "sudo apt-get update && sudo apt-get install -y"
UBUNTU_BASELINE_PACKAGES = ("git", "curl", "ca-certificates")
DOCKER_APT_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
DOCKER_APT_REPOSITORY_URL = "https://download.docker.com/linux/ubuntu"
DOCKER_APT_GPG_URL = f"{DOCKER_APT_REPOSITORY_URL}/gpg"
DOCKER_APT_GPG_KEYRING_PATH = "/etc/apt/keyrings/docker.asc"
DOCKER_APT_SOURCE_PATH = "/etc/apt/sources.list.d/docker.list"
DOCKER_APT_SUPPORTED_CODENAME = "noble"


@dataclass(frozen=True)
class HostPrerequisiteCheck:
    name: str
    status: Literal["pass", "fail"]
    detail: str
    package_name: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "detail": self.detail,
            "name": self.name,
            "package_name": self.package_name,
            "status": self.status,
        }


@dataclass(frozen=True)
class HostPrerequisiteResult:
    outcome: Literal["noop", "missing_prerequisites", "unsupported_host"]
    remediation_eligible: bool
    install_command: str | None
    missing_packages: tuple[str, ...]
    docker_bootstrap_required: bool
    checks: tuple[HostPrerequisiteCheck, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "docker_bootstrap_required": self.docker_bootstrap_required,
            "install_command": self.install_command,
            "missing_packages": list(self.missing_packages),
            "notes": list(self.notes),
            "outcome": self.outcome,
            "remediation_eligible": self.remediation_eligible,
        }


class HostPrerequisiteBackend(Protocol):
    def package_installed(self, package_name: str) -> bool: ...

    def docker_daemon_reachable(self) -> bool: ...

    def install_packages(self, package_names: tuple[str, ...]) -> None: ...

    def bootstrap_docker_engine(self) -> None: ...

    def ensure_docker_daemon(self) -> None: ...


class UbuntuAptHostPrerequisiteBackend:
    """Default subprocess-backed prerequisite detector for Ubuntu 24.04 hosts."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_package_state = {
            "git": _optional_bool(values, "HOST_PREREQ_GIT_INSTALLED"),
            "curl": _optional_bool(values, "HOST_PREREQ_CURL_INSTALLED"),
            "ca-certificates": _optional_bool(values, "HOST_PREREQ_CA_CERTIFICATES_INSTALLED"),
        }
        self._forced_docker_installed = _optional_bool(values, "HOST_PREREQ_DOCKER_INSTALLED")
        if self._forced_docker_installed is None:
            self._forced_docker_installed = _optional_bool(
                values, "HOST_PREREQ_DOCKER_IO_INSTALLED"
            )
        if self._forced_docker_installed is None:
            self._forced_docker_installed = _optional_bool(values, "HOST_DOCKER_INSTALLED")
        self._forced_docker_daemon = _optional_bool(values, "HOST_PREREQ_DOCKER_DAEMON_REACHABLE")
        if self._forced_docker_daemon is None:
            self._forced_docker_daemon = _optional_bool(values, "HOST_DOCKER_DAEMON_REACHABLE")

    def package_installed(self, package_name: str) -> bool:
        forced_value = self._forced_package_state.get(package_name)
        if forced_value is not None:
            return forced_value

        dpkg_query = shutil.which("dpkg-query")
        if dpkg_query is None:
            return False
        result = subprocess.run(
            [dpkg_query, "-W", "-f=${Status}", package_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "install ok installed"

    def docker_daemon_reachable(self) -> bool:
        if self._forced_docker_daemon is not None:
            return self._forced_docker_daemon

        docker_binary = shutil.which("docker")
        if docker_binary is None:
            return False
        result = subprocess.run(
            [docker_binary, "info", "--format", "{{json .ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def install_packages(self, package_names: tuple[str, ...]) -> None:
        if not package_names:
            return
        self._run_privileged_command(
            ("apt-get", "update"),
            failure_detail="refreshing apt package indexes",
        )
        self._run_privileged_command(
            ("apt-get", "install", "-y", *package_names),
            failure_detail=(f"installing baseline host prerequisites ({', '.join(package_names)})"),
        )

    def bootstrap_docker_engine(self) -> None:
        self._run_privileged_command(
            ("install", "-m", "0755", "-d", "/etc/apt/keyrings"),
            failure_detail="creating /etc/apt/keyrings for the Docker apt repository",
        )
        self._run_privileged_command(
            ("curl", "-fsSL", DOCKER_APT_GPG_URL, "-o", DOCKER_APT_GPG_KEYRING_PATH),
            failure_detail="downloading the Docker apt repository signing key",
        )
        self._run_privileged_command(
            ("chmod", "a+r", DOCKER_APT_GPG_KEYRING_PATH),
            failure_detail="setting Docker apt repository key permissions",
        )
        self._run_privileged_command(
            ("tee", DOCKER_APT_SOURCE_PATH),
            failure_detail="writing the Docker apt repository source list",
            input_text=_docker_apt_source_line() + "\n",
        )
        self._run_privileged_command(
            ("apt-get", "update"),
            failure_detail="refreshing apt package indexes after configuring the Docker repository",
        )
        self._run_privileged_command(
            ("apt-get", "install", "-y", *DOCKER_APT_PACKAGES),
            failure_detail=(
                f"installing Docker Engine packages ({', '.join(DOCKER_APT_PACKAGES)})"
            ),
        )

    def ensure_docker_daemon(self) -> None:
        self._run_privileged_command(
            ("systemctl", "enable", "--now", "docker"),
            failure_detail="starting and enabling the Docker daemon",
        )

    def _run_privileged_command(
        self,
        command: tuple[str, ...],
        *,
        failure_detail: str,
        input_text: str | None = None,
    ) -> None:
        result = subprocess.run(
            list(_privileged_command(command)),
            check=False,
            capture_output=True,
            input=input_text,
            text=True,
        )
        if result.returncode == 0:
            return

        combined_output = "\n".join(
            part.strip() for part in (result.stderr, result.stdout) if part.strip()
        )
        if _looks_like_privilege_failure(combined_output):
            raise StateValidationError(
                "Baseline host prerequisite remediation requires apt/systemd privileges; "
                "rerun dokploy-wizard install as root or with sudo."
            )

        detail = combined_output or f"command exited with status {result.returncode}"
        raise StateValidationError(
            f"Host prerequisite remediation failed while {failure_detail}: {detail}"
        )


def assess_host_prerequisites(
    *,
    host_facts: HostFacts,
    backend: HostPrerequisiteBackend,
) -> HostPrerequisiteResult:
    os_check = _os_support_check(host_facts)
    if os_check.status == "fail":
        return HostPrerequisiteResult(
            outcome="unsupported_host",
            remediation_eligible=False,
            install_command=None,
            missing_packages=(),
            docker_bootstrap_required=False,
            checks=(os_check,),
            notes=("Baseline apt remediation is only supported for Ubuntu 24.04 hosts.",),
        )

    checks = [os_check]
    missing_packages: list[str] = []
    for package_name in UBUNTU_BASELINE_PACKAGES:
        package_check = _package_check(package_name, backend)
        checks.append(package_check)
        if package_check.status == "fail":
            missing_packages.append(package_name)

    docker_cli_check = _docker_cli_check(host_facts)
    checks.append(docker_cli_check)
    docker_daemon_check = _docker_daemon_check(backend)
    checks.append(docker_daemon_check)
    docker_bootstrap_required = docker_cli_check.status == "fail"

    if (
        not missing_packages
        and not docker_bootstrap_required
        and docker_daemon_check.status == "pass"
    ):
        return HostPrerequisiteResult(
            outcome="noop",
            remediation_eligible=True,
            install_command=None,
            missing_packages=(),
            docker_bootstrap_required=False,
            checks=tuple(checks),
            notes=("Baseline Ubuntu 24.04 host prerequisites are already satisfied.",),
        )

    notes = []
    install_command: str | None = None
    if missing_packages:
        notes.append("Missing apt-managed baseline packages can be remediated on this host.")
    if docker_bootstrap_required:
        notes.append(
            "Docker Engine can be bootstrapped with the official Ubuntu apt repository on this host."
        )
    if docker_daemon_check.status == "fail":
        notes.append("Docker daemon reachability is required before install can proceed.")
    install_command = _build_install_command(
        missing_packages=tuple(missing_packages),
        docker_bootstrap_required=docker_bootstrap_required,
        docker_daemon_unreachable=docker_daemon_check.status == "fail",
    )

    return HostPrerequisiteResult(
        outcome="missing_prerequisites",
        remediation_eligible=True,
        install_command=install_command,
        missing_packages=tuple(missing_packages),
        docker_bootstrap_required=docker_bootstrap_required,
        checks=tuple(checks),
        notes=tuple(notes),
    )


def remediate_host_prerequisites(
    *,
    assessment: HostPrerequisiteResult,
    backend: HostPrerequisiteBackend,
) -> None:
    if not assessment.remediation_eligible or assessment.outcome != "missing_prerequisites":
        return
    if assessment.missing_packages:
        backend.install_packages(assessment.missing_packages)
    if assessment.docker_bootstrap_required:
        backend.bootstrap_docker_engine()
    if any(check.name == "docker_daemon" and check.status == "fail" for check in assessment.checks):
        backend.ensure_docker_daemon()


def _os_support_check(host_facts: HostFacts) -> HostPrerequisiteCheck:
    if host_facts.distribution_id != SUPPORTED_OS_ID:
        return HostPrerequisiteCheck(
            name="os_support",
            status="fail",
            detail=(
                f"unsupported host OS '{host_facts.distribution_id} {host_facts.version_id}'; "
                f"expected Ubuntu {SUPPORTED_OS_VERSION} LTS"
            ),
        )
    if not _is_supported_ubuntu_version(host_facts.version_id):
        return HostPrerequisiteCheck(
            name="os_support",
            status="fail",
            detail=(
                f"unsupported Ubuntu version '{host_facts.version_id}'; "
                f"expected {SUPPORTED_OS_VERSION}"
            ),
        )
    return HostPrerequisiteCheck(
        name="os_support",
        status="pass",
        detail=f"Ubuntu {SUPPORTED_OS_VERSION} host detected for apt-backed prerequisite checks.",
    )


def _package_check(package_name: str, backend: HostPrerequisiteBackend) -> HostPrerequisiteCheck:
    if not backend.package_installed(package_name):
        return HostPrerequisiteCheck(
            name=package_name.replace("-", "_").replace(".", "_"),
            status="fail",
            detail=f"required Ubuntu package '{package_name}' is not installed",
            package_name=package_name,
        )
    return HostPrerequisiteCheck(
        name=package_name.replace("-", "_").replace(".", "_"),
        status="pass",
        detail=f"Ubuntu package '{package_name}' is installed.",
        package_name=package_name,
    )


def _docker_cli_check(host_facts: HostFacts) -> HostPrerequisiteCheck:
    if not host_facts.docker_installed:
        return HostPrerequisiteCheck(
            name="docker_cli",
            status="fail",
            detail="Docker CLI is not installed on the host",
        )
    return HostPrerequisiteCheck(
        name="docker_cli",
        status="pass",
        detail="Docker CLI is available.",
    )


def _docker_daemon_check(backend: HostPrerequisiteBackend) -> HostPrerequisiteCheck:
    if not backend.docker_daemon_reachable():
        return HostPrerequisiteCheck(
            name="docker_daemon",
            status="fail",
            detail="Docker daemon is unavailable or unreachable",
            package_name="docker",
        )
    return HostPrerequisiteCheck(
        name="docker_daemon",
        status="pass",
        detail="Docker daemon responded successfully.",
        package_name="docker",
    )


def _build_install_command(
    *,
    missing_packages: tuple[str, ...],
    docker_bootstrap_required: bool,
    docker_daemon_unreachable: bool,
) -> str | None:
    commands: list[str] = []
    if missing_packages:
        commands.append(f"{APT_INSTALL_PREFIX} {' '.join(missing_packages)}")
    if docker_bootstrap_required:
        commands.extend(
            [
                "sudo install -m 0755 -d /etc/apt/keyrings",
                f"sudo curl -fsSL {DOCKER_APT_GPG_URL} -o {DOCKER_APT_GPG_KEYRING_PATH}",
                f"sudo chmod a+r {DOCKER_APT_GPG_KEYRING_PATH}",
                (
                    "sudo tee "
                    f"{DOCKER_APT_SOURCE_PATH} >/dev/null <<'EOF'\n{_docker_apt_source_line()}\nEOF"
                ),
                "sudo apt-get update",
                f"sudo apt-get install -y {' '.join(DOCKER_APT_PACKAGES)}",
            ]
        )
    if docker_daemon_unreachable:
        commands.append("sudo systemctl enable --now docker")
    if not commands:
        return None
    return " && ".join(commands)


def _docker_apt_source_line() -> str:
    return (
        f"deb [arch={_docker_apt_architecture()} "
        f"signed-by={DOCKER_APT_GPG_KEYRING_PATH}] "
        f"{DOCKER_APT_REPOSITORY_URL} {DOCKER_APT_SUPPORTED_CODENAME} stable"
    )


def _docker_apt_architecture() -> str:
    dpkg_binary = shutil.which("dpkg")
    if dpkg_binary is not None:
        result = subprocess.run(
            [dpkg_binary, "--print-architecture"],
            check=False,
            capture_output=True,
            text=True,
        )
        architecture = result.stdout.strip()
        if result.returncode == 0 and architecture and " " not in architecture:
            return architecture

    machine = os.uname().machine.lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine)


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = f"Invalid boolean value for '{key}': {raw_value!r}."
    raise StateValidationError(msg)


def _privileged_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if os.geteuid() == 0:
        return command
    sudo_binary = shutil.which("sudo")
    if sudo_binary is None:
        raise StateValidationError(
            "Baseline host prerequisite remediation requires apt/systemd privileges; "
            "rerun dokploy-wizard install as root or with sudo."
        )
    return (sudo_binary, *command)


def _looks_like_privilege_failure(output: str) -> bool:
    normalized = output.lower()
    return any(
        token in normalized
        for token in (
            "permission denied",
            "are you root",
            "must be root",
            "not in the sudoers",
            "sudo: a password is required",
            "sudo password",
            "a terminal is required",
        )
    )
