# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dokploy_wizard.cli import run_install_flow, run_uninstall_flow
from dokploy_wizard.core import SHARED_NETWORK_RESOURCE_TYPE, SHARED_POSTGRES_RESOURCE_TYPE
from dokploy_wizard.core.reconciler import SHARED_MAIL_RELAY_RESOURCE_TYPE
from dokploy_wizard.lifecycle.drift import LifecycleDriftError, validate_preserved_phases
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.inspection import build_live_drift_report

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"
_UNUSED_BACKEND = cast(Any, SimpleNamespace())


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _replace_line(content: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return content + f"\n{key}={value}\n"


def _seed_both_enabled_state(state_dir: Path) -> None:
    raw_input = parse_env_file(FIXTURES_DIR / "moodle-docuseal.env")
    desired_state = resolve_desired_state(raw_input)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "moodle",
                "docuseal",
            ),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=desired_state.format_version,
            resources=(
                OwnedResource(
                    "cloudflare_tunnel",
                    "moodle-docuseal-stack-tunnel",
                    "account:account-123",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-dokploy.example.com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-moodle.example.com",
                    "zone:zone-123:moodle.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-docuseal.example.com",
                    "zone:zone-123:docuseal.example.com",
                ),
                OwnedResource(
                    SHARED_NETWORK_RESOURCE_TYPE,
                    "moodle-docuseal-stack-core",
                    "stack:moodle-docuseal-stack:shared-network",
                ),
                OwnedResource(
                    SHARED_POSTGRES_RESOURCE_TYPE,
                    "moodle-docuseal-stack-postgres",
                    "stack:moodle-docuseal-stack:shared-postgres",
                ),
                OwnedResource(
                    "moodle_service",
                    "moodle-docuseal-stack-moodle",
                    "stack:moodle-docuseal-stack:moodle:service",
                ),
                OwnedResource(
                    "moodle_data",
                    "moodle-docuseal-stack-moodle-data",
                    "stack:moodle-docuseal-stack:moodle:data",
                ),
                OwnedResource(
                    "docuseal_service",
                    "moodle-docuseal-stack-docuseal",
                    "stack:moodle-docuseal-stack:docuseal:service",
                ),
                OwnedResource(
                    "docuseal_data",
                    "moodle-docuseal-stack-docuseal-data",
                    "stack:moodle-docuseal-stack:docuseal:data",
                ),
            ),
        ),
    )


def test_install_dry_run_plans_both_enabled_moodle_and_docuseal(tmp_path: Path) -> None:
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "moodle-docuseal.env",
        state_dir=tmp_path / "state",
        dry_run=True,
    )

    assert summary["lifecycle"]["mode"] == "install"
    assert summary["desired_state"]["enabled_packs"] == ["docuseal", "moodle"]
    assert [
        allocation["pack_name"] for allocation in summary["desired_state"]["shared_core"]["allocations"]
    ] == ["docuseal", "moodle"]
    assert summary["moodle"]["health_check"] == {
        "url": "https://moodle.example.com/login/index.php",
        "passed": None,
    }
    assert summary["docuseal"]["health_state"] == {
        "url": "https://docuseal.example.com/up",
        "path": "/up",
        "passed": None,
    }
    assert summary["docuseal"]["bootstrap_state"] == {
        "initialized": None,
        "secret_key_base_secret_ref": "moodle-docuseal-stack-docuseal-secret-key-base",
    }


def test_inspect_state_report_includes_both_enabled_moodle_and_docuseal_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(parse_env_file(FIXTURES_DIR / "moodle-docuseal.env"))
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                SHARED_POSTGRES_RESOURCE_TYPE,
                "moodle-docuseal-stack-postgres",
                "stack:moodle-docuseal-stack:shared-postgres",
            ),
            OwnedResource(
                SHARED_MAIL_RELAY_RESOURCE_TYPE,
                "moodle-docuseal-stack-postfix",
                "stack:moodle-docuseal-stack:shared-postfix",
            ),
            OwnedResource(
                "moodle_service",
                "moodle-docuseal-stack-moodle",
                "stack:moodle-docuseal-stack:moodle:service",
            ),
            OwnedResource(
                "docuseal_service",
                "moodle-docuseal-stack-docuseal",
                "stack:moodle-docuseal-stack:docuseal:service",
            ),
        ),
    )

    monkeypatch.setattr("dokploy_wizard.state.inspection._docker_cli_available", lambda: True)
    monkeypatch.setattr(
        "dokploy_wizard.state.inspection._list_docker_services",
        lambda: (
            "moodle-docuseal-stack-shared-postgres",
            "moodle-docuseal-stack-shared-postfix",
            "moodle-docuseal-stack-moodle",
            "moodle-docuseal-stack-docuseal",
        ),
    )
    monkeypatch.setattr(
        "dokploy_wizard.state.inspection._list_docker_containers", lambda: ()
    )
    monkeypatch.setattr(
        "dokploy_wizard.state.inspection._list_service_task_statuses",
        lambda service_name: (f"{service_name} running",),
    )
    monkeypatch.setattr(
        "dokploy_wizard.state.inspection._inspect_host_route_files",
        lambda desired_state: {  # noqa: ARG005
            "available": True,
            "detail": "No host-local routes matched requested hostnames.",
            "entries": [],
        },
    )

    report = build_live_drift_report(desired_state=desired_state, ownership_ledger=ownership_ledger)

    managed = {(entry["pack"], entry["live_name"]): entry for entry in report["entries"]}
    assert managed[("shared-core", "moodle-docuseal-stack-shared-postfix")][
        "classification"
    ] == "wizard_managed"
    assert managed[("shared-core", "moodle-docuseal-stack-shared-postfix")]["health"] == "healthy"
    assert managed[("moodle", "moodle-docuseal-stack-moodle")]["classification"] == "wizard_managed"
    assert managed[("moodle", "moodle-docuseal-stack-moodle")]["health"] == "healthy"
    assert managed[("docuseal", "moodle-docuseal-stack-docuseal")]["classification"] == "wizard_managed"
    assert managed[("docuseal", "moodle-docuseal-stack-docuseal")]["health"] == "healthy"


def test_retain_uninstall_preserves_moodle_and_docuseal_data_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_both_enabled_state(state_dir)
    confirm_file = tmp_path / "retain.confirm"
    confirm_file.write_text(
        "# Retain-mode confirmation for moodle-docuseal-stack\n"
        "Uninstall moodle-docuseal-stack and retain data\n",
        encoding="utf-8",
    )

    summary = run_uninstall_flow(
        state_dir=state_dir,
        destroy_data=False,
        dry_run=False,
        non_interactive=True,
        confirm_file=confirm_file,
    )

    deleted_types = {item["resource_type"] for item in summary["deleted_resources"]}
    retained_types = {item["resource_type"] for item in summary["retained_resources"]}
    assert summary["mode"] == "retain"
    assert summary["remaining_completed_steps"] == ["preflight", "dokploy_bootstrap"]
    assert {
        "cloudflare_tunnel",
        "cloudflare_dns_record",
        SHARED_NETWORK_RESOURCE_TYPE,
        "moodle_service",
        "docuseal_service",
    }.issubset(deleted_types)
    assert retained_types == {
        SHARED_POSTGRES_RESOURCE_TYPE,
        "moodle_data",
        "docuseal_data",
    }


def test_cli_install_rejects_hostname_conflict_for_both_enabled_fixture(tmp_path: Path) -> None:
    conflicted_env = tmp_path / "hostname-conflict.env"
    conflicted_env.write_text(
        _replace_line(
            _replace_line(
                (FIXTURES_DIR / "moodle-docuseal.env").read_text(encoding="utf-8"),
                "MOODLE_SUBDOMAIN",
                "apps",
            ),
            "DOCUSEAL_SUBDOMAIN",
            "apps",
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        "install",
        "--env-file",
        str(conflicted_env),
        "--state-dir",
        str(tmp_path / "state"),
        "--dry-run",
    )

    assert result.returncode != 0
    assert "Hostname collision" in result.stderr


def test_validate_preserved_phases_rejects_uninitialized_docuseal_bootstrap_for_both_enabled_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(parse_env_file(FIXTURES_DIR / "moodle-docuseal.env"))
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_docuseal",
        lambda **_: SimpleNamespace(
            result=SimpleNamespace(
                outcome="already_present",
                service=SimpleNamespace(action="reuse_owned", resource_id="svc-docuseal", resource_name="moodle-docuseal-stack-docuseal"),
                persistent_data=SimpleNamespace(action="reuse_owned", resource_id="vol-docuseal", resource_name="moodle-docuseal-stack-docuseal-data"),
                bootstrap_state=SimpleNamespace(
                    initialized=False,
                    secret_key_base_secret_ref="moodle-docuseal-stack-docuseal-secret-key-base",
                ),
                health_state=SimpleNamespace(
                    url="https://docuseal.example.com/up",
                    path="/up",
                    passed=None,
                ),
            )
        ),
    )

    with pytest.raises(LifecycleDriftError, match="DocuSeal bootstrap state is not initialized"):
        validate_preserved_phases(
            raw_env=parse_env_file(FIXTURES_DIR / "moodle-docuseal.env"),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("docuseal",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=_UNUSED_BACKEND,
            coder_backend=_UNUSED_BACKEND,
            docuseal_backend=cast(Any, SimpleNamespace(check_health=lambda *, service, url: True)),
        )
