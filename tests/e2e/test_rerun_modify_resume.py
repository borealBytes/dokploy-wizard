# mypy: ignore-errors
# ruff: noqa: E501
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard.cli import run_install_flow, run_modify_flow
from dokploy_wizard.core import SharedCoreResourceRecord
from dokploy_wizard.lifecycle import applicable_phases_for
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

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"
FIXTURES_DIR = REPO_ROOT / "fixtures"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _write_env(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _replace_line(content: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return content + f"\n{key}={value}\n"


def _seed_lifecycle_state(
    state_dir: Path,
    *,
    env_path: Path,
    completed_steps: tuple[str, ...] | None = None,
) -> None:
    raw_input = parse_env_file(env_path)
    desired_state = resolve_desired_state(raw_input)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=completed_steps or applicable_phases_for(desired_state),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=desired_state.format_version, resources=()),
    )


class FakeSharedCoreBackend:
    def __init__(self, *, allocations_ready: bool) -> None:
        self.allocations_ready = allocations_ready
        self.network = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-core",
            resource_name="moodle-docuseal-stack-shared",
        )
        self.postgres = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-postgres",
            resource_name="moodle-docuseal-stack-shared-postgres",
        )
        self.litellm = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-litellm",
            resource_name="moodle-docuseal-stack-shared-litellm",
        )

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        self.network = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-core",
            resource_name=resource_name,
        )
        return self.network

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.postgres.resource_id == resource_id:
            return self.postgres
        return None

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.postgres.resource_name == resource_name:
            return self.postgres
        return None

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.postgres = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-postgres",
            resource_name=resource_name,
        )
        return self.postgres

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        raise AssertionError(f"unexpected redis create: {resource_name}")

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-postfix",
            resource_name=resource_name,
        )

    def get_litellm_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.litellm.resource_id == resource_id:
            return self.litellm
        return None

    def find_litellm_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.litellm.resource_name == resource_name:
            return self.litellm
        return None

    def create_litellm_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.litellm = SharedCoreResourceRecord(
            resource_id="moodle-docuseal-stack-litellm",
            resource_name=resource_name,
        )
        return self.litellm

    def validate_postgres_allocations(self, allocations: tuple[object, ...]) -> bool:
        assert allocations
        return self.allocations_ready


def _seed_moodle_docuseal_state(state_dir: Path) -> Path:
    base_env = (FIXTURES_DIR / "moodle-docuseal.env").read_text(encoding="utf-8")
    env_path = _write_env(
        state_dir.parent / "moodle-docuseal-rerun.env",
        base_env
        + "\nCLOUDFLARE_MOCK_EXISTING_TUNNEL_ID=moodle-docuseal-stack-tunnel"
        + "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,moodle.example.com,docuseal.example.com\n",
    )
    _seed_lifecycle_state(state_dir, env_path=env_path)
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    "cloudflare_tunnel",
                    "moodle-docuseal-stack-tunnel",
                    "account:account-123",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dokploy-example-com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "moodle-example-com",
                    "zone:zone-123:moodle.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "docuseal-example-com",
                    "zone:zone-123:docuseal.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "moodle-docuseal-stack-core",
                    "stack:moodle-docuseal-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "moodle-docuseal-stack-postgres",
                    "stack:moodle-docuseal-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_litellm",
                    "moodle-docuseal-stack-litellm",
                    "stack:moodle-docuseal-stack:shared-litellm",
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
    return env_path


def test_cli_install_then_rerun_surfaces_explicit_noop(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    base_env = (
        _replace_line(
            (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
            "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID",
            "lifecycle-stack-tunnel",
        )
        + "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com"
        + "\nHEADSCALE_MOCK_EXISTING_SERVICE_ID=lifecycle-stack-headscale\n"
    )
    rerun_env = _write_env(tmp_path / "rerun.env", base_env)
    _seed_lifecycle_state(state_dir, env_path=rerun_env)
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    "cloudflare_tunnel",
                    "lifecycle-stack-tunnel",
                    "account:account-123",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dokploy-example-com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "headscale-example-com",
                    "zone:zone-123:headscale.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "lifecycle-stack-shared",
                    "stack:lifecycle-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "lifecycle-stack-shared-postgres",
                    "stack:lifecycle-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_litellm",
                    "lifecycle-stack-shared-litellm",
                    "stack:lifecycle-stack:shared-litellm",
                ),
                OwnedResource(
                    "headscale_service",
                    "lifecycle-stack-headscale",
                    "stack:lifecycle-stack:headscale",
                ),
            ),
        ),
    )

    result = _run_cli(
        "install",
        "--env-file",
        str(rerun_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["lifecycle"]["mode"] == "noop"
    assert payload["state_status"] == "existing"


def test_cli_modify_domain_rejects_mock_contamination_for_live_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    install_env = _write_env(
        tmp_path / "install.env",
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
    )
    _seed_lifecycle_state(state_dir, env_path=install_env)
    modify_env = _write_env(
        tmp_path / "modify.env",
        _replace_line(
            (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
            "ROOT_DOMAIN",
            "example.net",
        ),
    )
    modified = _run_cli(
        "modify",
        "--env-file",
        str(modify_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert modified.returncode != 0
    assert "live/pre-live runs require real integrations" in modified.stderr
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in modified.stderr
    assert "DOKPLOY_MOCK_API_MODE" in modified.stderr


def test_cli_inspect_state_persists_live_drift_snapshot(tmp_path: Path) -> None:
    state_dir = tmp_path / "inspect-state"

    result = _run_cli(
        "inspect-state",
        "--env-file",
        str(FIXTURES_DIR / "lifecycle-headscale.env"),
        "--state-dir",
        str(state_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["live_drift"]["status"] in {"clean", "drift_detected", "unavailable"}
    assert payload["live_drift"]["summary"] == {
        "wizard_managed": payload["live_drift"]["summary"]["wizard_managed"],
        "manual_collision": payload["live_drift"]["summary"]["manual_collision"],
        "host_local_route": payload["live_drift"]["summary"]["host_local_route"],
        "unknown_unmanaged": payload["live_drift"]["summary"]["unknown_unmanaged"],
    }
    assert payload["live_drift"]["inspection"]["docker"]["available"] in {True, False}
    assert payload["live_drift"]["inspection"]["host_routes"]["available"] in {True, False}
    assert isinstance(payload["live_drift"]["entries"], list)
    assert all(
        entry["classification"]
        in {
            "wizard_managed",
            "manual_collision",
            "host_local_route",
            "unknown_unmanaged",
        }
        for entry in payload["live_drift"]["entries"]
    )
    if payload["live_drift"]["entries"]:
        assert all("detail" in entry for entry in payload["live_drift"]["entries"])
        assert all("classification" in entry for entry in payload["live_drift"]["entries"])
    assert payload["live_drift"]["detected"] == any(
        entry["classification"] != "wizard_managed" or entry.get("health") != "healthy"
        for entry in payload["live_drift"]["entries"]
    )
    assert set(payload["live_drift"]["summary"]) == {
        "wizard_managed",
        "manual_collision",
        "host_local_route",
        "unknown_unmanaged",
    }
    assert json.loads((state_dir / "desired-state.json").read_text(encoding="utf-8")) == payload
    assert (
        json.loads((state_dir / "raw-input.json").read_text(encoding="utf-8"))["format_version"]
        == 1
    )
    assert not (state_dir / "applied-state.json").exists()
    assert not (state_dir / "ownership-ledger.json").exists()


def test_cli_resume_rejects_persisted_mock_reuse_before_mutation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    base_env = (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8")
    resume_env = _write_env(
        tmp_path / "resume.env",
        base_env,
    )
    _seed_lifecycle_state(
        state_dir,
        env_path=resume_env,
        completed_steps=("preflight", "dokploy_bootstrap", "networking"),
    )
    raw_input_before = (state_dir / "raw-input.json").read_text(encoding="utf-8")
    desired_before = (state_dir / "desired-state.json").read_text(encoding="utf-8")
    applied_before = (state_dir / "applied-state.json").read_text(encoding="utf-8")

    resumed = _run_cli(
        "install",
        "--env-file",
        str(resume_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert resumed.returncode != 0
    assert "live/pre-live runs require real integrations" in resumed.stderr
    assert "HEADSCALE_MOCK_HEALTHY" in resumed.stderr
    assert (state_dir / "raw-input.json").read_text(encoding="utf-8") == raw_input_before
    assert (state_dir / "desired-state.json").read_text(encoding="utf-8") == desired_before
    assert (state_dir / "applied-state.json").read_text(encoding="utf-8") == applied_before


def test_cli_modify_rejects_unsupported_stack_name_change(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    install_env = _write_env(
        tmp_path / "install.env",
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
    )
    _seed_lifecycle_state(state_dir, env_path=install_env)
    modified = _run_cli(
        "modify",
        "--env-file",
        str(FIXTURES_DIR / "modify-unsupported.env"),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert modified.returncode != 0
    assert "STACK_NAME changes are unsupported" in modified.stderr


def test_cli_install_rejects_legacy_checkpoint_contract_with_nonempty_progress(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    resume_env = _write_env(
        tmp_path / "resume.env",
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
    )
    raw_input = parse_env_file(resume_env)
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
                "cloudflare_access",
            ),
            lifecycle_checkpoint_contract_version=1,
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=desired_state.format_version,
            resources=(
                OwnedResource(
                    "cloudflare_tunnel",
                    "legacy-tunnel",
                    "account:account-123",
                ),
            ),
        ),
    )

    resumed = _run_cli(
        "install",
        "--env-file",
        str(resume_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert resumed.returncode != 0
    assert "lifecycle checkpoint contract version 1" in resumed.stderr
    assert "Only empty install scaffolds can be restarted" in resumed.stderr


def test_run_install_rerun_noop_surfaces_both_enabled_moodle_and_docuseal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    state_dir = tmp_path / "state"
    env_path = _seed_moodle_docuseal_state(state_dir)

    summary = run_install_flow(
        env_file=env_path,
        state_dir=state_dir,
        dry_run=True,
        shared_core_backend=FakeSharedCoreBackend(allocations_ready=True),
    )

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["moodle"]["outcome"] == "already_present"
    assert summary["moodle"]["health_check"]["url"] == "https://moodle.example.com/login/index.php"
    assert summary["docuseal"]["outcome"] == "already_present"
    assert summary["docuseal"]["health_state"] == {
        "url": "https://docuseal.example.com/up",
        "path": "/up",
        "passed": None,
    }


def test_run_modify_admin_change_keeps_moodle_and_docuseal_seed_only_after_successful_install(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    state_dir = tmp_path / "state"
    env_path = _seed_moodle_docuseal_state(state_dir)
    modify_env = _write_env(
        tmp_path / "modify.env",
        _replace_line(env_path.read_text(encoding="utf-8"), "DOKPLOY_ADMIN_PASSWORD", "EvenSaferPass123"),
    )

    with pytest.raises(
        ValueError,
        match="Requested modify operation changes values that are not modeled as supported runtime mutations",
    ):
        run_modify_flow(
            env_file=modify_env,
            state_dir=state_dir,
            dry_run=True,
            shared_core_backend=FakeSharedCoreBackend(allocations_ready=True),
        )
