# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.state import (
    OwnedResource,
    OwnershipLedger,
    StateValidationError,
    load_state_dir,
    validate_existing_state,
)


def test_ownership_ledger_rejects_duplicate_resource_identity() -> None:
    with pytest.raises(StateValidationError, match="duplicate resource identity"):
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="dns_record",
                    resource_id="dokploy.example.com",
                    scope="example.com",
                ),
                OwnedResource(
                    resource_type="dns_record",
                    resource_id="dokploy.example.com",
                    scope="example.com",
                ),
            ),
        )


def test_load_state_dir_rejects_corrupt_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ownership-ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "resources": [
                    {
                        "resource_type": "dns_record",
                        "resource_id": "dokploy.example.com",
                        "scope": "example.com",
                    },
                    {
                        "resource_type": "dns_record",
                        "resource_id": "dokploy.example.com",
                        "scope": "example.com",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateValidationError, match="ownership-ledger.json"):
        load_state_dir(tmp_path)


def test_load_state_dir_rejects_mixed_version_applied_state(tmp_path: Path) -> None:
    applied_state_path = tmp_path / "applied-state.json"
    applied_state_path.write_text(
        json.dumps(
            {
                "format_version": 99,
                "desired_state_fingerprint": "abc123",
                "completed_steps": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateValidationError, match="applied-state.json"):
        load_state_dir(tmp_path)


def test_validate_existing_state_accepts_historical_seaweedfs_checkpoint(tmp_path: Path) -> None:
    fingerprint = "dc4bc86fdd3879263c3d9ebf075a71e3dad324f44cd42343bc8654d40e8b7e89"

    (tmp_path / "raw-input.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "values": {"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "desired-state.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "root_domain": "openmerge.me",
                "stack_name": "openmerge",
                "dokploy_url": "https://dokploy.openmerge.me",
                "dokploy_api_url": "http://127.0.0.1:3000",
                "enabled_features": ["dokploy", "headscale"],
                "selected_packs": ["headscale", "seaweedfs"],
                "enabled_packs": ["headscale", "seaweedfs"],
                "hostnames": {
                    "dokploy": "dokploy.openmerge.me",
                    "headscale": "headscale.openmerge.me",
                    "s3": "s3.openmerge.me",
                },
                "enable_tailscale": False,
                "tailscale_hostname": None,
                "tailscale_enable_ssh": False,
                "tailscale_tags": [],
                "tailscale_subnet_routes": [],
                "cloudflare_access_otp_emails": [],
                "shared_core": {
                    "network_name": "openmerge-shared",
                    "postgres": None,
                    "redis": None,
                    "allocations": [],
                },
                "seaweedfs_access_key": "seaweed-access",
                "seaweedfs_secret_key": "seaweed-secret",
                "openclaw_channels": ["matrix"],
                "openclaw_replicas": 1,
                "my_farm_advisor_channels": ["matrix"],
                "my_farm_advisor_replicas": 1,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "applied-state.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "desired_state_fingerprint": fingerprint,
                "completed_steps": [
                    "preflight",
                    "dokploy_bootstrap",
                    "networking",
                    "cloudflare_access",
                    "shared_core",
                    "headscale",
                    "matrix",
                    "nextcloud",
                    "seaweedfs",
                    "openclaw",
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ownership-ledger.json").write_text(
        json.dumps({"format_version": 1, "resources": []}),
        encoding="utf-8",
    )

    loaded_state = load_state_dir(tmp_path)

    assert validate_existing_state(loaded_state) is True
    assert loaded_state.applied_state is not None
    assert "seaweedfs" in loaded_state.applied_state.completed_steps
