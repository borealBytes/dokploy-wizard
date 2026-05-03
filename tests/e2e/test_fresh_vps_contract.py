from __future__ import annotations

import json
from pathlib import Path

from dokploy_wizard.state import OwnedResource, OwnershipLedger, write_ownership_ledger

from .test_rerun_modify_resume import (
    FIXTURES_DIR,
    _replace_line,
    _run_cli,
    _seed_lifecycle_state,
    _write_env,
)


def test_cli_install_fresh_state_rejects_mock_prelive_run_without_writing_state(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    install_env = _write_env(
        tmp_path / "install.env",
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
    )

    result = _run_cli(
        "install",
        "--env-file",
        str(install_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert result.returncode != 0
    assert "live/pre-live runs require real integrations" in result.stderr
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in result.stderr
    assert "DOKPLOY_MOCK_API_MODE" in result.stderr
    assert "HEADSCALE_MOCK_HEALTHY" in result.stderr
    assert not (state_dir / "raw-input.json").exists()
    assert not (state_dir / "desired-state.json").exists()
    assert not (state_dir / "applied-state.json").exists()
    assert not (state_dir / "ownership-ledger.json").exists()


def test_cli_install_same_host_completed_state_returns_explicit_noop_without_mutation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    rerun_env = _write_env(
        tmp_path / "rerun.env",
        _replace_line(
            (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
            "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID",
            "lifecycle-stack-tunnel",
        )
        + "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com"
        + "\nHEADSCALE_MOCK_EXISTING_SERVICE_ID=lifecycle-stack-headscale\n",
    )
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
    env_before = rerun_env.read_text(encoding="utf-8")
    raw_before = (state_dir / "raw-input.json").read_text(encoding="utf-8")
    desired_before = (state_dir / "desired-state.json").read_text(encoding="utf-8")
    applied_before = (state_dir / "applied-state.json").read_text(encoding="utf-8")
    ledger_before = (state_dir / "ownership-ledger.json").read_text(encoding="utf-8")

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
    assert payload["lifecycle"]["phases_to_run"] == []
    assert payload["lifecycle"]["start_phase"] is None
    assert payload["lifecycle"]["reasons"] == [
        "Requested raw input and desired state match the persisted target."
    ]
    assert (
        payload["lifecycle"]["initial_completed_steps"] == payload["lifecycle"]["applicable_phases"]
    )
    assert payload["lifecycle"]["preserved_phases"] == payload["lifecycle"]["applicable_phases"]
    assert payload["lifecycle"]["applicable_phases"] == [
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    ]
    assert payload["state_status"] == "existing"
    assert payload["bootstrap"]["outcome"] == "already_present"
    assert payload["networking"]["outcome"] == "already_present"
    assert payload["shared_core"]["outcome"] == "already_present"
    assert payload["headscale"]["outcome"] == "not_run"
    assert rerun_env.read_text(encoding="utf-8") == env_before
    assert (state_dir / "raw-input.json").read_text(encoding="utf-8") == raw_before
    assert (state_dir / "desired-state.json").read_text(encoding="utf-8") == desired_before
    assert (state_dir / "applied-state.json").read_text(encoding="utf-8") == applied_before
    assert (state_dir / "ownership-ledger.json").read_text(encoding="utf-8") == ledger_before
