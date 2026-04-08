# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

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
    assert report.advisories == (
        "Host looks like a local or bare-metal machine; "
        "this is advisory only if it meets the same baseline.",
    )


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
