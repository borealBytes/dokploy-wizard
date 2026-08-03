from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.state import OwnedResource, StateValidationError
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
)
from dokploy_wizard.state.upgrade import StateUpgradeError, upgrade_state_contract


def _state_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "raw_input": tmp_path / "raw-input.json",
        "desired": tmp_path / "desired-state.json",
        "applied": tmp_path / "applied-state.json",
        "ledger": tmp_path / "ownership-ledger.json",
        "litellm_keys": tmp_path / "litellm-generated-keys.json",
        "surfsense_secrets": tmp_path / "surfsense-generated-secrets.json",
        "seaweedfs_secrets": tmp_path / "seaweedfs-generated-secrets.json",
    }


def _seed_paths(paths: dict[str, Path]) -> None:
    for index, path in enumerate(paths.values()):
        path.write_bytes(f"bytes-{index}".encode())


def test_upgrade_preserves_raw_and_secret_bytes_and_rejects_stale_cas(tmp_path: Path) -> None:
    paths = _state_paths(tmp_path)
    _seed_paths(paths)

    intent = upgrade_state_contract(
        state_dir=tmp_path,
        owner_id="3b8e1e83-0e57-4d66-a65e-1edbf2aac838",
        paths=paths,
    )

    assert intent.status == "complete"
    assert paths["raw_input"].read_bytes() == b"bytes-0"
    assert paths["litellm_keys"].read_bytes() == b"bytes-4"
    with pytest.raises(StateUpgradeError, match="CAS mismatch"):
        upgrade_state_contract(
            state_dir=tmp_path,
            owner_id="3b8e1e83-0e57-4d66-a65e-1edbf2aac838",
            paths=paths,
            expected_generation=0,
        )


def test_sync_persistence_models_round_trip_and_reject_unknown_fields() -> None:
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard", compose_id="compose-1", owner_id=owner_id
    )
    desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=spec,
    )
    applied = AppliedSyncState.initial(desired)
    metadata = SyncOwnershipMetadata(
        owner_id=owner_id,
        action_provenance="created",
        remote_fingerprint="c" * 64,
        spec_hash="d" * 64,
        physical_target_id="schedule-1",
        creation_receipt_sha256="e" * 64,
        preimage_receipt_sha256=None,
        deletion_policy="delete",
    )

    assert SyncDesiredState.from_dict(desired.to_dict()) == desired
    assert AppliedSyncState.from_dict(applied.to_dict()) == applied
    assert SyncOwnershipMetadata.from_dict(metadata.to_dict()) == metadata
    with pytest.raises(SyncStateError, match="exact keys"):
        SyncDesiredState.from_dict({**desired.to_dict(), "unexpected": True})


def test_owned_resource_binds_sync_provenance_metadata() -> None:
    metadata = SyncOwnershipMetadata(
        owner_id="3b8e1e83-0e57-4d66-a65e-1edbf2aac838",
        action_provenance="created",
        remote_fingerprint="a" * 64,
        spec_hash="b" * 64,
        physical_target_id="schedule-1",
        creation_receipt_sha256="c" * 64,
        preimage_receipt_sha256=None,
        deletion_policy="delete",
    )
    resource = OwnedResource(
        resource_type="shared_core_sync_schedule",
        resource_id="schedule-1",
        scope="stack:wizard:shared-sync",
        metadata=metadata,
    )

    assert resource.to_dict()["metadata"] == metadata.to_dict()
    with pytest.raises(StateValidationError, match="exact keys"):
        OwnedResource.from_dict({**resource.to_dict(), "unexpected": "value"})


def test_state_upgrade_resumes_an_interrupted_fsynced_write_sequence(tmp_path: Path) -> None:
    paths = _state_paths(tmp_path)
    _seed_paths(paths)
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    completed = upgrade_state_contract(state_dir=tmp_path, owner_id=owner_id, paths=paths)
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    interrupted = json.loads(intent_path.read_text(encoding="utf-8"))
    interrupted["generation"] = completed.generation - 1
    interrupted["status"] = "writing"
    interrupted["completed_writes"] = ["owner"]
    intent_path.write_text(json.dumps(interrupted), encoding="utf-8")

    resumed = upgrade_state_contract(
        state_dir=tmp_path,
        owner_id=owner_id,
        paths=paths,
        expected_generation=completed.generation - 1,
    )

    assert resumed.status == "complete"
    assert resumed.completed_writes == ("owner", "desired", "applied", "ledger")
