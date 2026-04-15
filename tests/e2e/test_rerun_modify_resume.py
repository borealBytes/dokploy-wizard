from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
