from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def _seed_state_dir(state_dir: Path) -> None:
    raw_input = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    desired_state = resolve_desired_state(raw_input)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "headscale",
                "nextcloud",
            ),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource("cloudflare_tunnel", "nextcloud-stack-tunnel", "account:account-123"),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-dokploy.example.com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "nextcloud-stack-core",
                    "stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "nextcloud-stack-postgres",
                    "stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_redis",
                    "nextcloud-stack-redis",
                    "stack:nextcloud-stack:shared-redis",
                ),
                OwnedResource(
                    "headscale_service",
                    "nextcloud-stack-headscale",
                    "stack:nextcloud-stack:headscale",
                ),
                OwnedResource(
                    "nextcloud_service",
                    "nextcloud-stack-nextcloud",
                    "stack:nextcloud-stack:nextcloud-service",
                ),
                OwnedResource(
                    "onlyoffice_service",
                    "nextcloud-stack-onlyoffice",
                    "stack:nextcloud-stack:onlyoffice-service",
                ),
                OwnedResource(
                    "nextcloud_volume",
                    "nextcloud-stack-nextcloud-data",
                    "stack:nextcloud-stack:nextcloud-volume",
                ),
                OwnedResource(
                    "onlyoffice_volume",
                    "nextcloud-stack-onlyoffice-data",
                    "stack:nextcloud-stack:onlyoffice-volume",
                ),
            ),
        ),
    )


def test_cli_destroy_mode_rejects_weak_confirmation_without_mutation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_state_dir(state_dir)
    ledger_before = json.loads((state_dir / "ownership-ledger.json").read_text(encoding="utf-8"))

    result = _run_cli(
        "uninstall",
        "--destroy-data",
        "--state-dir",
        str(state_dir),
        "--non-interactive",
        "--confirm-file",
        str(FIXTURES_DIR / "weak.confirm"),
    )

    assert result.returncode != 0
    assert "weak confirmation phrases" in result.stderr.lower()
    ledger_after = json.loads((state_dir / "ownership-ledger.json").read_text(encoding="utf-8"))
    assert ledger_after == ledger_before
