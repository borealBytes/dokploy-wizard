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
    checks: tuple[HostPrerequisiteCheck, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
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

    def ensure_docker_daemon(self) -> None: ...


class UbuntuAptHostPrerequisiteBackend:
    """Default subprocess-backed prerequisite detector for Ubuntu 24.04 hosts."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_package_state = {
            "git": _optional_bool(values, "HOST_PREREQ_GIT_INSTALLED"),
            "curl": _optional_bool(values, "HOST_PREREQ_CURL_INSTALLED"),
            "ca-certificates": _optional_bool(values, "HOST_PREREQ_CA_CERTIFICATES_INSTALLED"),
            "docker.io": _optional_bool(values, "HOST_PREREQ_DOCKER_IO_INSTALLED"),
        }
        if self._forced_package_state["docker.io"] is None:
            self._forced_package_state["docker.io"] = _optional_bool(
                values, "HOST_DOCKER_INSTALLED"
            )
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

    def ensure_docker_daemon(self) -> None:
        self._run_privileged_command(
            ("systemctl", "enable", "--now", "docker"),
            failure_detail="starting and enabling the Docker daemon",
        )

    def _run_privileged_command(self, command: tuple[str, ...], *, failure_detail: str) -> None:
        result = subprocess.run(
            list(_privileged_command(command)),
            check=False,
            capture_output=True,
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
            checks=(os_check,),
            notes=("Baseline apt remediation is only supported for Ubuntu 24.04 hosts.",),
        )

    checks = [os_check]
    missing_packages: list[str] = []
    for package_name in ("git", "curl", "ca-certificates", "docker.io"):
        package_check = _package_check(package_name, backend)
        checks.append(package_check)
        if package_check.status == "fail":
            missing_packages.append(package_name)

    docker_daemon_check = _docker_daemon_check(backend)
    checks.append(docker_daemon_check)

    if not missing_packages and docker_daemon_check.status == "pass":
        return HostPrerequisiteResult(
            outcome="noop",
            remediation_eligible=True,
            install_command=None,
            missing_packages=(),
            checks=tuple(checks),
            notes=("Baseline Ubuntu 24.04 host prerequisites are already satisfied.",),
        )

    notes = []
    install_command: str | None = None
    if missing_packages:
        install_command = f"{APT_INSTALL_PREFIX} {' '.join(missing_packages)}"
        notes.append("Missing apt-managed baseline packages can be remediated on this host.")
    if docker_daemon_check.status == "fail":
        notes.append("Docker daemon reachability is required before install can proceed.")

    return HostPrerequisiteResult(
        outcome="missing_prerequisites",
        remediation_eligible=True,
        install_command=install_command,
        missing_packages=tuple(missing_packages),
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


def _docker_daemon_check(backend: HostPrerequisiteBackend) -> HostPrerequisiteCheck:
    if not backend.docker_daemon_reachable():
        return HostPrerequisiteCheck(
            name="docker_daemon",
            status="fail",
            detail="Docker daemon is unavailable or unreachable",
            package_name="docker.io",
        )
    return HostPrerequisiteCheck(
        name="docker_daemon",
        status="pass",
        detail="Docker daemon responded successfully.",
        package_name="docker.io",
    )


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
