# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.cli import run_uninstall_flow
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _write_confirm_file(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
                    "cloudflare_dns_record",
                    "dns-headscale.example.com",
                    "zone:zone-123:headscale.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-nextcloud.example.com",
                    "zone:zone-123:nextcloud.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-onlyoffice.example.com",
                    "zone:zone-123:onlyoffice.example.com",
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
                    "shared_core_litellm",
                    "nextcloud-stack-litellm",
                    "stack:nextcloud-stack:shared-litellm",
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


def test_retain_uninstall_preserves_data_resources_and_shrinks_checkpoint(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_state_dir(state_dir)
    confirm_file = _write_confirm_file(
        tmp_path / "retain.confirm",
        "# Retain-mode confirmation for nextcloud-stack",
        "Uninstall nextcloud-stack and retain data",
    )

    summary = run_uninstall_flow(
        state_dir=state_dir,
        destroy_data=False,
        dry_run=False,
        non_interactive=True,
        confirm_file=confirm_file,
    )

    loaded = load_state_dir(state_dir)
    assert summary["mode"] == "retain"
    assert summary["state_cleared"] is False
    assert summary["remaining_completed_steps"] == ["preflight", "dokploy_bootstrap"]
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == ("preflight", "dokploy_bootstrap")
    assert loaded.ownership_ledger is not None
    assert {resource.resource_type for resource in loaded.ownership_ledger.resources} == {
        "shared_core_postgres",
        "shared_core_redis",
        "nextcloud_volume",
        "onlyoffice_volume",
    }


def test_destroy_uninstall_clears_state_when_nothing_owned_remains(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_state_dir(state_dir)
    confirm_file = _write_confirm_file(
        tmp_path / "destroy.confirm",
        "# Destroy-mode confirmation for nextcloud-stack",
        "I understand this is destructive",
        "Destroy data for this environment",
        "Destroy all data for nextcloud-stack",
    )

    summary = run_uninstall_flow(
        state_dir=state_dir,
        destroy_data=True,
        dry_run=False,
        non_interactive=True,
        confirm_file=confirm_file,
    )

    loaded = load_state_dir(state_dir)
    assert summary["mode"] == "destroy"
    assert summary["state_cleared"] is True
    assert loaded.raw_input is None
    assert loaded.desired_state is None
    assert loaded.applied_state is None
    assert loaded.ownership_ledger is None
