# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.host_prereqs import (
    APT_INSTALL_PREFIX,
    UbuntuAptHostPrerequisiteBackend,
    assess_host_prerequisites,
)
from dokploy_wizard.preflight import (
    CORE_PROFILE,
    FULL_PACK_SET_PROFILE,
    PreflightError,
    collect_host_facts,
    derive_required_profile,
    run_preflight,
)
from dokploy_wizard.state import parse_env_file, resolve_desired_state

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def test_supported_core_host_passes_preflight_with_local_advisory() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    desired_state = resolve_desired_state(raw_env)
    host_facts = collect_host_facts(raw_env)

    report = run_preflight(desired_state, host_facts)

    assert report.required_profile == CORE_PROFILE
    assert host_facts.cpu_count == 2
    assert host_facts.memory_gb == 4
    assert host_facts.disk_gb == 40
    assert str(host_facts.disk_path) in {"/", "/var/lib/docker"}
    assert tuple((check.name, check.status, check.detail) for check in report.checks[:3]) == (
        ("os_support", "pass", "Ubuntu 24.04 host detected."),
        ("docker_installed", "pass", "Docker CLI is available."),
        ("docker_daemon", "pass", "Docker daemon responded successfully."),
    )
    assert report.advisories == (
        "Host looks like a local or bare-metal machine; "
        "this is advisory only if it meets the same baseline.",
    )


def test_ubuntu_24_04_patch_release_is_supported() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    desired_state = resolve_desired_state(raw_env)
    values = dict(raw_env.values)
    values["HOST_OS_VERSION_ID"] = "24.04.2"
    host_facts = collect_host_facts(
        type(raw_env)(format_version=raw_env.format_version, values=values)
    )

    report = run_preflight(desired_state, host_facts)

    assert report.checks[0].name == "os_support"
    assert report.checks[0].status == "pass"


def test_disk_override_can_include_explicit_storage_path(tmp_path: Path) -> None:
    env_file = tmp_path / "disk-path.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=test-stack",
                "ROOT_DOMAIN=example.com",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=2",
                "HOST_MEMORY_GB=4",
                "HOST_DISK_GB=200",
                "HOST_DISK_PATH=/var/lib/docker",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    raw_env = parse_env_file(env_file)
    host_facts = collect_host_facts(raw_env)

    assert host_facts.disk_gb == 200
    assert host_facts.disk_path == "/var/lib/docker"


def test_unsupported_host_fixture_fails_fast() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "unsupported-host.env")
    desired_state = resolve_desired_state(raw_env)
    host_facts = collect_host_facts(raw_env)

    with pytest.raises(PreflightError, match="unsupported host OS 'debian 12'"):
        run_preflight(desired_state, host_facts)


def test_missing_docker_fails_with_operator_action() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    desired_state = resolve_desired_state(raw_env)
    values = dict(raw_env.values)
    values["HOST_DOCKER_INSTALLED"] = "false"
    values["HOST_DOCKER_DAEMON_REACHABLE"] = "false"
    host_facts = collect_host_facts(
        type(raw_env)(format_version=raw_env.format_version, values=values)
    )

    with pytest.raises(
        PreflightError,
        match="Docker is not installed; install Docker before running dokploy-wizard install",
    ):
        run_preflight(desired_state, host_facts)


def test_full_pack_set_requires_full_profile_resources(tmp_path: Path) -> None:
    env_file = tmp_path / "full-pack-shortfall.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=full-pack-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_HEADSCALE=true",
                "ENABLE_MATRIX=true",
                "ENABLE_NEXTCLOUD=true",
                "ENABLE_OPENCLAW=true",
                "OPENCLAW_CHANNELS=matrix,telegram",
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
                "DOKPLOY_BOOTSTRAP_HEALTHY=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    raw_env = parse_env_file(env_file)
    desired_state = resolve_desired_state(raw_env)

    assert derive_required_profile(desired_state) == FULL_PACK_SET_PROFILE

    with pytest.raises(PreflightError, match="insufficient CPU for Full Pack Set"):
        run_preflight(desired_state, collect_host_facts(raw_env))


def test_full_pack_set_memory_shortfall_warns_at_11_gb_and_passes_at_12_gb(tmp_path: Path) -> None:
    env_file = tmp_path / "full-pack-memory-boundary.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=full-pack-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_HEADSCALE=true",
                "ENABLE_MATRIX=true",
                "ENABLE_NEXTCLOUD=true",
                "ENABLE_OPENCLAW=true",
                "OPENCLAW_CHANNELS=matrix,telegram",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=6",
                "HOST_MEMORY_GB=11",
                "HOST_DISK_GB=150",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    raw_env = parse_env_file(env_file)
    desired_state = resolve_desired_state(raw_env)

    with pytest.raises(PreflightError, match="insufficient memory for Full Pack Set: need 12 GB"):
        run_preflight(desired_state, collect_host_facts(raw_env))

    warning_report = run_preflight(
        desired_state,
        collect_host_facts(raw_env),
        allow_memory_shortfall=True,
    )
    assert warning_report.required_profile == FULL_PACK_SET_PROFILE
    assert any(
        check.name == "memory"
        and check.status == "warn"
        and check.detail == "insufficient memory for Full Pack Set: need 12 GB, found 11 GB"
        for check in warning_report.checks
    )
    assert warning_report.has_only_memory_shortfall_warning() is True

    values = dict(raw_env.values)
    values["HOST_MEMORY_GB"] = "12"
    passing_report = run_preflight(
        desired_state,
        collect_host_facts(type(raw_env)(format_version=raw_env.format_version, values=values)),
        allow_memory_shortfall=True,
    )
    assert any(
        check.name == "memory"
        and check.status == "pass"
        and check.detail == "Memory meets the Full Pack Set profile."
        for check in passing_report.checks
    )


def test_port_collisions_fail_preflight() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    desired_state = resolve_desired_state(raw_env)
    values = dict(raw_env.values)
    values["HOST_PORT_443_IN_USE"] = "true"
    host_facts = collect_host_facts(
        type(raw_env)(format_version=raw_env.format_version, values=values)
    )

    with pytest.raises(PreflightError, match=r"required ports already in use: \[443\]"):
        run_preflight(desired_state, host_facts)


def test_host_prerequisites_report_noop_when_baseline_is_already_satisfied() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    values = dict(raw_env.values)
    values["HOST_PREREQ_GIT_INSTALLED"] = "true"
    values["HOST_PREREQ_CURL_INSTALLED"] = "true"
    values["HOST_PREREQ_CA_CERTIFICATES_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_IO_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_DAEMON_REACHABLE"] = "true"
    raw_env = type(raw_env)(format_version=raw_env.format_version, values=values)

    result = assess_host_prerequisites(
        host_facts=collect_host_facts(raw_env),
        backend=UbuntuAptHostPrerequisiteBackend(raw_env),
    )

    assert result.outcome == "noop"
    assert result.remediation_eligible is True
    assert result.install_command is None
    assert result.missing_packages == ()
    assert tuple((check.name, check.status) for check in result.checks) == (
        ("os_support", "pass"),
        ("git", "pass"),
        ("curl", "pass"),
        ("ca_certificates", "pass"),
        ("docker_io", "pass"),
        ("docker_daemon", "pass"),
    )
    assert result.to_dict()["outcome"] == "noop"


def test_host_prerequisites_report_missing_packages_individually() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    values = dict(raw_env.values)
    values["HOST_PREREQ_GIT_INSTALLED"] = "false"
    values["HOST_PREREQ_CURL_INSTALLED"] = "true"
    values["HOST_PREREQ_CA_CERTIFICATES_INSTALLED"] = "false"
    values["HOST_PREREQ_DOCKER_IO_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_DAEMON_REACHABLE"] = "true"
    raw_env = type(raw_env)(format_version=raw_env.format_version, values=values)

    result = assess_host_prerequisites(
        host_facts=collect_host_facts(raw_env),
        backend=UbuntuAptHostPrerequisiteBackend(raw_env),
    )

    assert result.outcome == "missing_prerequisites"
    assert result.remediation_eligible is True
    assert result.missing_packages == ("git", "ca-certificates")
    assert result.install_command == f"{APT_INSTALL_PREFIX} git ca-certificates"
    assert tuple(check.package_name for check in result.checks if check.status == "fail") == (
        "git",
        "ca-certificates",
    )


@pytest.mark.parametrize(
    ("override_key", "missing_package"),
    (
        ("HOST_PREREQ_CURL_INSTALLED", "curl"),
        ("HOST_PREREQ_DOCKER_IO_INSTALLED", "docker.io"),
    ),
)
def test_host_prerequisites_report_single_missing_package_directly(
    override_key: str,
    missing_package: str,
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    values = dict(raw_env.values)
    values["HOST_PREREQ_GIT_INSTALLED"] = "true"
    values["HOST_PREREQ_CURL_INSTALLED"] = "true"
    values["HOST_PREREQ_CA_CERTIFICATES_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_IO_INSTALLED"] = "true"
    values[override_key] = "false"
    values["HOST_PREREQ_DOCKER_DAEMON_REACHABLE"] = "true"
    raw_env = type(raw_env)(format_version=raw_env.format_version, values=values)

    result = assess_host_prerequisites(
        host_facts=collect_host_facts(raw_env),
        backend=UbuntuAptHostPrerequisiteBackend(raw_env),
    )

    assert result.outcome == "missing_prerequisites"
    assert result.remediation_eligible is True
    assert result.missing_packages == (missing_package,)
    assert result.install_command == f"{APT_INSTALL_PREFIX} {missing_package}"
    assert tuple(check.package_name for check in result.checks if check.status == "fail") == (
        missing_package,
    )


def test_host_prerequisites_mark_unsupported_hosts_as_not_apt_remediable() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "unsupported-host.env")

    result = assess_host_prerequisites(
        host_facts=collect_host_facts(raw_env),
        backend=UbuntuAptHostPrerequisiteBackend(raw_env),
    )

    assert result.outcome == "unsupported_host"
    assert result.remediation_eligible is False
    assert result.install_command is None
    assert result.missing_packages == ()
    assert tuple((check.name, check.status) for check in result.checks) == (("os_support", "fail"),)


def test_host_prerequisites_require_reachable_docker_daemon_for_noop() -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "core-low-resource.env")
    values = dict(raw_env.values)
    values["HOST_PREREQ_GIT_INSTALLED"] = "true"
    values["HOST_PREREQ_CURL_INSTALLED"] = "true"
    values["HOST_PREREQ_CA_CERTIFICATES_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_IO_INSTALLED"] = "true"
    values["HOST_PREREQ_DOCKER_DAEMON_REACHABLE"] = "false"
    raw_env = type(raw_env)(format_version=raw_env.format_version, values=values)

    result = assess_host_prerequisites(
        host_facts=collect_host_facts(raw_env),
        backend=UbuntuAptHostPrerequisiteBackend(raw_env),
    )

    assert result.outcome == "missing_prerequisites"
    assert result.remediation_eligible is True
    assert result.missing_packages == ()
    assert result.install_command is None
    assert result.checks[-1].name == "docker_daemon"
    assert result.checks[-1].status == "fail"
