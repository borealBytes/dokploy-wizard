from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.lifecycle.changes import applicable_phases_for, classify_install_request
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
)
from dokploy_wizard.state.upgrade import StateUpgradeError, upgrade_state_contract

_OWNER_ID = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"


def _sync_desired() -> SyncDesiredState:
    schedule = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=_OWNER_ID,
    )
    return SyncDesiredState.from_schedule(
        owner_id=_OWNER_ID,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=schedule,
    )


def test_nested_desired_and_applied_sync_blocks_round_trip_with_explicit_nulls() -> None:
    base = resolve_desired_state(parse_env_file(_FIXTURE))
    sync_desired = _sync_desired()
    desired = replace(base, opencode_go_sync=sync_desired)
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=desired.fingerprint(),
        completed_steps=(),
        opencode_go_sync=AppliedSyncState.initial(sync_desired),
    )

    desired_payload = desired.to_dict()
    applied_payload = applied.to_dict()

    assert desired_payload["shared_core"]["opencode_go_sync"] == sync_desired.to_dict()
    assert applied_payload["opencode_go_sync"]["dokploy_schedule_id"] is None
    assert type(desired).from_dict(desired_payload) == desired
    assert type(applied).from_dict(applied_payload) == applied


def test_true_pre_task5_completed_state_preserves_old_fingerprint_and_reconciles() -> None:
    base_raw = parse_env_file(_FIXTURE)
    raw = replace(base_raw, values={**base_raw.values, "PACKS": "nextcloud,coder"})
    desired = resolve_desired_state(raw)
    legacy_payload = desired.to_dict()
    legacy_payload.pop("runtime_images")
    legacy_payload["shared_core"].pop("opencode_go_sync", None)
    encoded = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
    legacy_fingerprint = sha256(encoded.encode()).hexdigest()
    applied_payload = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=legacy_fingerprint,
        completed_steps=applicable_phases_for(desired),
    ).to_dict()
    applied_payload.pop("runtime_images", None)
    applied_payload.pop("opencode_go_sync", None)

    parsed = type(desired).from_dict(legacy_payload)
    applied = AppliedStateCheckpoint.from_dict(applied_payload)
    plan = classify_install_request(
        existing_raw=raw,
        existing_desired=parsed,
        existing_applied=applied,
        requested_raw=raw,
        requested_desired=desired,
    )

    assert parsed.opencode_go_sync is None
    assert legacy_fingerprint != desired.fingerprint()
    assert plan.mode == "resume"
    assert plan.start_phase == "shared_core"
    assert "coder" in plan.phases_to_run


@pytest.mark.parametrize("provenance", ["reused", "legacy_unproven"])
def test_preserve_provenance_forbids_destructive_receipts(provenance: str) -> None:
    with pytest.raises(SyncStateError, match="preserve"):
        SyncOwnershipMetadata(
            owner_id=_OWNER_ID,
            action_provenance=provenance,
            remote_fingerprint="a" * 64,
            spec_hash="b" * 64,
            physical_target_id="schedule-1",
            creation_receipt_sha256="c" * 64,
            preimage_receipt_sha256=None,
            deletion_policy="preserve",
        )


def test_state_upgrade_augments_outer_v1_and_records_legacy_to_sync_fingerprint(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    legacy_desired_payload = desired.to_dict()
    legacy_desired_payload.pop("runtime_images")
    legacy_desired_payload["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(
            legacy_desired_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    paths = {
        "raw_input": tmp_path / "raw-input.json",
        "desired": tmp_path / "desired-state.json",
        "applied": tmp_path / "applied-state.json",
        "ledger": tmp_path / "ownership-ledger.json",
        "litellm_keys": tmp_path / "litellm-generated-keys.json",
        "surfsense_secrets": tmp_path / "surfsense-generated-secrets.json",
        "seaweedfs_secrets": tmp_path / "seaweedfs-generated-secrets.json",
    }
    paths["raw_input"].write_text(json.dumps(raw.to_dict()), encoding="utf-8")
    paths["desired"].write_text(json.dumps(legacy_desired_payload), encoding="utf-8")
    paths["applied"].write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint=legacy_fingerprint,
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    paths["ledger"].write_text(
        json.dumps(OwnershipLedger(format_version=1, resources=()).to_dict()),
        encoding="utf-8",
    )
    for key in ("litellm_keys", "surfsense_secrets", "seaweedfs_secrets"):
        paths[key].write_bytes(f"protected-{key}".encode())
    for path in paths.values():
        path.chmod(0o600)
    protected = {key: paths[key].read_bytes() for key in paths}

    sync_desired = _sync_desired()
    intent = upgrade_state_contract(
        state_dir=tmp_path,
        owner_id=_OWNER_ID,
        paths=paths,
        sync_desired=sync_desired,
        sync_applied=AppliedSyncState.initial(sync_desired),
    )

    upgraded_desired = json.loads(paths["desired"].read_text(encoding="utf-8"))
    upgraded_applied = json.loads(paths["applied"].read_text(encoding="utf-8"))
    assert intent.status == "complete"
    assert upgraded_desired["shared_core"]["opencode_go_sync"]["owner_id"] == _OWNER_ID
    assert (
        upgraded_applied["opencode_go_sync"]["previous_legacy_fingerprint"]
        == legacy_fingerprint
    )
    for key in ("raw_input", "litellm_keys", "surfsense_secrets", "seaweedfs_secrets"):
        assert paths[key].read_bytes() == protected[key]


def test_state_upgrade_blocks_applied_fingerprint_mismatch_before_any_write(tmp_path: Path) -> None:
    paths = {
        key: tmp_path / name
        for key, name in {
            "raw_input": "raw-input.json",
            "desired": "desired-state.json",
            "applied": "applied-state.json",
            "ledger": "ownership-ledger.json",
            "litellm_keys": "litellm-generated-keys.json",
            "surfsense_secrets": "surfsense-generated-secrets.json",
            "seaweedfs_secrets": "seaweedfs-generated-secrets.json",
        }.items()
    }
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    paths["raw_input"].write_text(json.dumps(raw.to_dict()), encoding="utf-8")
    paths["desired"].write_text(json.dumps(desired.to_dict()), encoding="utf-8")
    paths["applied"].write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint="f" * 64,
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    paths["ledger"].write_text(
        json.dumps(OwnershipLedger(format_version=1, resources=()).to_dict()),
        encoding="utf-8",
    )
    for key in ("litellm_keys", "surfsense_secrets", "seaweedfs_secrets"):
        paths[key].write_text("protected", encoding="utf-8")
    for path in paths.values():
        path.chmod(0o600)
    before = {key: path.read_bytes() for key, path in paths.items()}

    with pytest.raises(StateUpgradeError, match="fingerprint"):
        sync_desired = _sync_desired()
        upgrade_state_contract(
            state_dir=tmp_path,
            owner_id=_OWNER_ID,
            paths=paths,
            sync_desired=sync_desired,
            sync_applied=AppliedSyncState.initial(sync_desired),
        )

    assert {key: path.read_bytes() for key, path in paths.items()} == before
