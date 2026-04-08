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
