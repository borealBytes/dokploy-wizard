from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_upgrade_host_a_coder import CoderUpgradeClient
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    derive_observed_totals,
    parse_host_a_observation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_process import (
    ProcessObservation,
    parse_modify_execution,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError


def test_modify_execution_rejects_success_without_authoritative_observation() -> None:
    # Given
    process = ProcessObservation(
        0,
        b"",
        (b'[remote:modify:stdout] {"lifecycle":{"mode":"noop","phases_to_run":[]}}\n'),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match="observation"):
        parse_modify_execution(process, remote_root=Path("/root/dokploy-wizard"))


def test_coder_upgrade_client_has_no_synthetic_completion_switch() -> None:
    # Given / When
    completion_switch = getattr(CoderUpgradeClient, "mark_upgrade_complete", None)

    # Then
    assert completion_switch is None


def _observation_payload() -> dict[str, proof.JsonValue]:
    archive_sha256 = "b" * 64
    return {
        "catalog": {
            "applied_model_set_sha256": "c" * 64,
            "generation_mode": 0o600,
            "generation_path": "/var/lib/docker/volumes/catalog/_data/generations/1-catalog.json",
            "generation_sha256": "c" * 64,
            "state_mode": 0o600,
            "state_output_sha256": "c" * 64,
            "state_path": "/var/lib/docker/volumes/catalog/_data/opencode-go-sync-state-v1.json",
        },
        "migration": {
            "mode": 0o600,
            "mutation_events": ["d" * 64],
            "path": (
                "/root/dokploy-wizard/state/coder-template-migration/"
                "coder-migration-receipts-v1.json"
            ),
            "primary_name": "ubuntu-vscode-opencode-pi",
            "push_names": [
                "ubuntu-vscode-hermes",
                "ubuntu-vscode-kdense-byok",
                "ubuntu-vscode-opencode-pi",
                "ubuntu-vscode-opencode-web",
            ],
            "receipt_sha256": "e" * 64,
            "retired_names": ["ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"],
            "status": "completed",
        },
        "release": {
            "active_release_path": f"/root/dokploy-wizard/releases/{archive_sha256}",
            "archive_sha256": archive_sha256,
            "commit_sha": "a" * 40,
            "manifest_mode": 0o644,
            "manifest_path": "/root/dokploy-wizard/release-manifest.json",
            "manifest_sha256": "f" * 64,
        },
        "schedule": {
            "applied_desired_sha256": "1" * 64,
            "desired_sha256": "1" * 64,
            "desired_spec_sha256": "2" * 64,
            "receipt_sha256": "3" * 64,
            "receipt_mode": 0o600,
            "receipt_path": (
                f"/root/dokploy-wizard/state/shared-core-sync/schedule-receipts/{'3' * 64}.json"
            ),
            "remote_spec_sha256": "2" * 64,
        },
        "schema_version": 1,
        "sync_results": [
            {
                "durable_write_count": 2,
                "mode": 0o600,
                "path": "/var/lib/docker/volumes/catalog/_data/lease-results/result.json",
                "result_sha256": "4" * 64,
            },
        ],
    }


def test_host_observation_rejects_manifest_path_or_mode_drift() -> None:
    # Given
    payload = _observation_payload()
    release = payload["release"]
    assert isinstance(release, dict)
    release["manifest_mode"] = 0o600

    # When / Then
    with pytest.raises(UpgradeHostAError, match="release manifest"):
        parse_host_a_observation(payload, remote_root=Path("/root/dokploy-wizard"))


def test_observed_totals_come_from_new_receipt_events() -> None:
    # Given
    before_payload = _observation_payload()
    before_migration = before_payload["migration"]
    assert isinstance(before_migration, dict)
    before_migration["mutation_events"] = []
    before_payload["sync_results"] = []
    before = parse_host_a_observation(
        before_payload,
        remote_root=Path("/root/dokploy-wizard"),
    )
    after = parse_host_a_observation(
        _observation_payload(),
        remote_root=Path("/root/dokploy-wizard"),
    )

    # When
    totals = derive_observed_totals(before, after)

    # Then
    assert totals.control_plane_mutations == 1
    assert totals.synchronizer_durable_writes == 2


def test_observed_totals_reject_receipt_event_loss() -> None:
    # Given
    before = parse_host_a_observation(
        _observation_payload(),
        remote_root=Path("/root/dokploy-wizard"),
    )
    after_payload = _observation_payload()
    after_migration = after_payload["migration"]
    assert isinstance(after_migration, dict)
    after_migration["mutation_events"] = []
    after = parse_host_a_observation(
        after_payload,
        remote_root=Path("/root/dokploy-wizard"),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match="regressed"):
        derive_observed_totals(before, after)
