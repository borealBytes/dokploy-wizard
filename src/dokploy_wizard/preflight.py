"""Host preflight checks for the Dokploy wizard install flow."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dokploy_wizard.packs.catalog import get_pack_definition
from dokploy_wizard.state import DesiredState, RawEnvInput, StateValidationError

SUPPORTED_OS_ID = "ubuntu"
SUPPORTED_OS_VERSION = "24.04"
REQUIRED_PORTS = (80, 443, 3000)


class PreflightError(RuntimeError):
    """Raised when host preflight fails."""


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    minimum_vcpu: int
    minimum_memory_gb: int
    minimum_disk_gb: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "minimum_disk_gb": self.minimum_disk_gb,
            "minimum_memory_gb": self.minimum_memory_gb,
            "minimum_vcpu": self.minimum_vcpu,
            "name": self.name,
        }


CORE_PROFILE = ResourceProfile("Core", minimum_vcpu=2, minimum_memory_gb=4, minimum_disk_gb=40)
RECOMMENDED_PROFILE = ResourceProfile(
    "Recommended", minimum_vcpu=4, minimum_memory_gb=8, minimum_disk_gb=100
)
FULL_PACK_SET_PROFILE = ResourceProfile(
    "Full Pack Set", minimum_vcpu=6, minimum_memory_gb=12, minimum_disk_gb=150
)


@dataclass(frozen=True)
class HostFacts:
    distribution_id: str
    version_id: str
    cpu_count: int
    memory_gb: int
    disk_gb: int
    disk_path: str
    docker_installed: bool
    docker_daemon_reachable: bool
    ports_in_use: tuple[int, ...]
    environment_classification: str
    hostname: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cpu_count": self.cpu_count,
            "disk_gb": self.disk_gb,
            "disk_path": str(self.disk_path),
            "distribution_id": self.distribution_id,
            "docker_daemon_reachable": self.docker_daemon_reachable,
            "docker_installed": self.docker_installed,
            "environment_classification": self.environment_classification,
            "hostname": self.hostname,
            "memory_gb": self.memory_gb,
            "ports_in_use": list(self.ports_in_use),
            "version_id": self.version_id,
        }


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(frozen=True)
class PreflightReport:
    host_facts: HostFacts
    required_profile: ResourceProfile
    checks: tuple[PreflightCheck, ...]
    advisories: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "advisories": list(self.advisories),
            "checks": [check.to_dict() for check in self.checks],
            "host_facts": self.host_facts.to_dict(),
            "required_profile": self.required_profile.to_dict(),
        }

    def failed_checks(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail")

    def warning_checks(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == "warn")

    def has_only_memory_shortfall_warning(self) -> bool:
        warning_checks = self.warning_checks()
        return (
            bool(warning_checks)
            and not self.failed_checks()
            and all(check.name == "memory" for check in warning_checks)
        )


def derive_required_profile(desired_state: DesiredState) -> ResourceProfile:
    recommended_packs = {
        pack_name
        for pack_name in desired_state.enabled_packs
        if get_pack_definition(pack_name).resource_profile == "recommended"
    }
    if {"matrix", "nextcloud"}.issubset(recommended_packs) and (
        "openclaw" in recommended_packs or "my-farm-advisor" in recommended_packs
    ):
        return FULL_PACK_SET_PROFILE
    if recommended_packs:
        return RECOMMENDED_PROFILE
    return CORE_PROFILE


def collect_host_facts(raw_env: RawEnvInput) -> HostFacts:
    values = raw_env.values
    os_release = _read_os_release()
    detected_ports_in_use = _list_ports_in_use()
    docker_installed = _override_bool(
        values,
        "HOST_DOCKER_INSTALLED",
        default=shutil.which("docker") is not None,
    )
    ports_in_use = tuple(
        sorted(
            port
            for port in REQUIRED_PORTS
            if _override_bool(
                values,
                f"HOST_PORT_{port}_IN_USE",
                default=port in detected_ports_in_use,
            )
        )
    )

    return HostFacts(
        disk_path=values.get("HOST_DISK_PATH", str(_read_disk_path())),
        distribution_id=values.get("HOST_OS_ID", os_release.get("ID", "unknown")).lower(),
        version_id=values.get("HOST_OS_VERSION_ID", os_release.get("VERSION_ID", "unknown")),
        cpu_count=_override_int(values, "HOST_CPU_COUNT", default=max(os.cpu_count() or 0, 0)),
        memory_gb=_override_int(values, "HOST_MEMORY_GB", default=_read_memory_gb()),
        disk_gb=_override_int(values, "HOST_DISK_GB", default=_read_disk_gb(_read_disk_path())),
        docker_installed=docker_installed,
        docker_daemon_reachable=_override_bool(
            values,
            "HOST_DOCKER_DAEMON_REACHABLE",
            default=_docker_daemon_reachable() if docker_installed else False,
        ),
        ports_in_use=ports_in_use,
        environment_classification=values.get(
            "HOST_ENVIRONMENT",
            _classify_environment(),
        ),
        hostname=values.get("HOST_HOSTNAME", socket.gethostname()),
    )


def run_preflight(
    desired_state: DesiredState,
    host_facts: HostFacts,
    allow_memory_shortfall: bool = False,
) -> PreflightReport:
    required_profile = derive_required_profile(desired_state)
    checks = (
        _os_check(host_facts),
        _docker_installation_check(host_facts),
        _docker_daemon_check(host_facts),
        _cpu_check(host_facts, required_profile),
        _memory_check(host_facts, required_profile),
        _disk_check(host_facts, required_profile),
        _ports_check(host_facts),
    )
    advisories = _build_advisories(host_facts)
    report = PreflightReport(
        host_facts=host_facts,
        required_profile=required_profile,
        checks=checks,
        advisories=advisories,
    )

    failures = [check.detail for check in report.failed_checks()]
    if not allow_memory_shortfall:
        failures.extend(check.detail for check in report.warning_checks())
    if failures:
        msg = "Preflight failed: " + "; ".join(failures)
        raise PreflightError(msg)

    return report


def _os_check(host_facts: HostFacts) -> PreflightCheck:
    if host_facts.distribution_id != SUPPORTED_OS_ID:
        return PreflightCheck(
            name="os_support",
            status="fail",
            detail=(
                f"unsupported host OS '{host_facts.distribution_id} {host_facts.version_id}'; "
                f"expected Ubuntu {SUPPORTED_OS_VERSION} LTS"
            ),
        )
    if host_facts.version_id != SUPPORTED_OS_VERSION:
        return PreflightCheck(
            name="os_support",
            status="fail",
            detail=(
                f"unsupported Ubuntu version '{host_facts.version_id}'; "
                f"expected {SUPPORTED_OS_VERSION}"
            ),
        )
    return PreflightCheck(
        name="os_support",
        status="pass",
        detail=f"Ubuntu {SUPPORTED_OS_VERSION} host detected.",
    )


def _docker_installation_check(host_facts: HostFacts) -> PreflightCheck:
    if not host_facts.docker_installed:
        return PreflightCheck(
            name="docker_installed",
            status="fail",
            detail="Docker is not installed; install Docker before running dokploy-wizard install",
        )
    return PreflightCheck(
        name="docker_installed",
        status="pass",
        detail="Docker CLI is available.",
    )


def _docker_daemon_check(host_facts: HostFacts) -> PreflightCheck:
    if not host_facts.docker_daemon_reachable:
        return PreflightCheck(
            name="docker_daemon",
            status="fail",
            detail="Docker daemon is unavailable or unreachable",
        )
    return PreflightCheck(
        name="docker_daemon",
        status="pass",
        detail="Docker daemon responded successfully.",
    )


def _cpu_check(host_facts: HostFacts, required_profile: ResourceProfile) -> PreflightCheck:
    if host_facts.cpu_count < required_profile.minimum_vcpu:
        return PreflightCheck(
            name="cpu",
            status="fail",
            detail=(
                f"insufficient CPU for {required_profile.name}: need "
                f"{required_profile.minimum_vcpu} vCPU, found {host_facts.cpu_count}"
            ),
        )
    return PreflightCheck(
        name="cpu",
        status="pass",
        detail=f"CPU meets the {required_profile.name} profile.",
    )


def _memory_check(host_facts: HostFacts, required_profile: ResourceProfile) -> PreflightCheck:
    if host_facts.memory_gb < required_profile.minimum_memory_gb:
        return PreflightCheck(
            name="memory",
            status="warn",
            detail=(
                f"insufficient memory for {required_profile.name}: need "
                f"{required_profile.minimum_memory_gb} GB, found {host_facts.memory_gb} GB"
            ),
        )
    return PreflightCheck(
        name="memory",
        status="pass",
        detail=f"Memory meets the {required_profile.name} profile.",
    )


def _disk_check(host_facts: HostFacts, required_profile: ResourceProfile) -> PreflightCheck:
    if host_facts.disk_gb < required_profile.minimum_disk_gb:
        return PreflightCheck(
            name="disk",
            status="fail",
            detail=(
                f"insufficient disk for {required_profile.name}: need "
                f"{required_profile.minimum_disk_gb} GB, found {host_facts.disk_gb} GB "
                f"on deployment storage {host_facts.disk_path}"
            ),
        )
    return PreflightCheck(
        name="disk",
        status="pass",
        detail=f"Disk meets the {required_profile.name} profile on {host_facts.disk_path}.",
    )


def _ports_check(host_facts: HostFacts) -> PreflightCheck:
    if host_facts.ports_in_use:
        return PreflightCheck(
            name="required_ports",
            status="fail",
            detail=f"required ports already in use: {list(host_facts.ports_in_use)}",
        )
    return PreflightCheck(
        name="required_ports",
        status="pass",
        detail="Ports 80, 443, and 3000 are available.",
    )


def _build_advisories(host_facts: HostFacts) -> tuple[str, ...]:
    normalized = host_facts.environment_classification.lower()
    if normalized in {"local", "bare-metal", "bare_metal", "desktop"}:
        return (
            "Host looks like a local or bare-metal machine; "
            "this is advisory only if it meets the same baseline.",
        )
    return ()


def _read_os_release() -> dict[str, str]:
    os_release_path = Path("/etc/os-release")
    values: dict[str, str] = {}
    if not os_release_path.exists():
        return values
    for raw_line in os_release_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def _read_memory_gb() -> int:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return 0
    for line in meminfo_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                total_kib = int(parts[1])
                return total_kib // (1024 * 1024)
    return 0


def _read_disk_gb(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free // (1024**3)


def _read_disk_path() -> Path:
    if shutil.which("docker") is None:
        return Path("/")
    result = subprocess.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        path = result.stdout.strip()
        if path:
            return Path(path)
    return Path("/")


def _docker_daemon_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _list_ports_in_use() -> set[int]:
    ss_binary = shutil.which("ss")
    if ss_binary is None:
        return {port for port in REQUIRED_PORTS if _localhost_port_open(port)}

    result = subprocess.run(
        [ss_binary, "-ltnH"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {port for port in REQUIRED_PORTS if _localhost_port_open(port)}

    ports: set[int] = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue
        local_address = columns[3]
        port_text = local_address.rsplit(":", maxsplit=1)[-1]
        if port_text.isdigit():
            ports.add(int(port_text))
    return ports


def _localhost_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _classify_environment() -> str:
    product_name_path = Path("/sys/devices/virtual/dmi/id/product_name")
    if product_name_path.exists():
        product_name = product_name_path.read_text(encoding="utf-8").strip().lower()
        virtual_markers = ("kvm", "virtual", "vmware", "droplet", "hvm domu")
        if any(marker in product_name for marker in virtual_markers):
            return "vps"
    return "local"


def _override_int(values: dict[str, str], key: str, *, default: int) -> int:
    raw_value = values.get(key)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        msg = f"Invalid integer value for '{key}': {raw_value!r}."
        raise StateValidationError(msg) from error
    if value < 0:
        msg = f"Invalid integer value for '{key}': must be non-negative."
        raise StateValidationError(msg)
    return value


def _override_bool(values: dict[str, str], key: str, *, default: bool) -> bool:
    raw_value = values.get(key)
    if raw_value is None:
        return default
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = f"Invalid boolean value for '{key}': {raw_value!r}."
    raise StateValidationError(msg)
