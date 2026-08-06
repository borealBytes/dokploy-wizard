from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.lifecycle.changes import applicable_phases_for
from dokploy_wizard.lifecycle.shared_core_sync import reconcile_and_persist_sync_projection
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncDesiredState,
    SyncOwnershipMetadata,
)
from dokploy_wizard.state.sync_schema import ScheduleSpec
from dokploy_wizard.state.upgrade import StateUpgradeError, planned_sync_owner_id
from dokploy_wizard.state.upgrade_io import atomic_json, read_json

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_INTENT = "state-upgrade-intent-v1.json"


@dataclass(frozen=True, slots=True)
class DisabledSyncBackend:
    def reconcile_sync_schedule(
        self,
        *,
        existing_applied: AppliedSyncState | None,
        existing_metadata: SyncOwnershipMetadata | None,
    ) -> None:
        assert existing_applied is None
        assert existing_metadata is None


@dataclass(frozen=True, slots=True)
class EnabledSyncBackend:
    outcome: SyncScheduleOutcome

    def reconcile_sync_schedule(
        self,
        *,
        existing_applied: AppliedSyncState | None,
        existing_metadata: SyncOwnershipMetadata | None,
    ) -> SyncScheduleOutcome:
        assert existing_applied is None
        assert existing_metadata is None
        return self.outcome


def _interrupted_runtime_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DesiredState, OwnershipLedger]:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    raw = replace(
        raw,
        values={**raw.values, "OPENCLAW_GATEWAY_TOKEN": "legacy-openclaw-token"},
    )
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    legacy_desired = desired.to_dict()
    legacy_desired["openclaw_gateway_token"] = "legacy-openclaw-token"
    legacy_desired["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (tmp_path / "desired-state.json").write_text(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=legacy_fingerprint,
            completed_steps=applicable_phases_for(desired),
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    original_atomic_json = atomic_json

    def interrupt_applied_write(path: Path, payload: dict[str, object]) -> None:
        if path.name == "applied-state.json":
            raise OSError("fixture interruption")
        original_atomic_json(path, payload)

    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", interrupt_applied_write)
    with pytest.raises(OSError, match="fixture interruption"):
        reconcile_and_persist_sync_projection(
            state_dir=tmp_path,
            backend=DisabledSyncBackend(),
            desired_state=desired,
            ownership_ledger=ledger,
        )
    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", original_atomic_json)
    return desired, ledger


def _resume(tmp_path: Path, desired: DesiredState, ledger: OwnershipLedger) -> None:
    reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=DisabledSyncBackend(),
        desired_state=desired,
        ownership_ledger=ledger,
    )


def _intent_payload(tmp_path: Path) -> dict[str, object]:
    return read_json(tmp_path / _INTENT)


def test_runtime_resume_rejects_missing_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    (tmp_path / _INTENT).unlink()

    with pytest.raises(StateUpgradeError, match="fingerprint mismatch"):
        _resume(tmp_path, desired, ledger)


def test_runtime_resume_rejects_malformed_preimage_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    pre_hashes = intent["pre_hashes"]
    assert isinstance(pre_hashes, dict)
    pre_hashes["desired"] = "malformed"
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")

    with pytest.raises(StateUpgradeError, match="recovery intent hashes"):
        _resume(tmp_path, desired, ledger)


def test_runtime_resume_rejects_stale_desired_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    stale_desired = read_json(tmp_path / "desired-state.json")
    stale_desired["openclaw_gateway_token"] = "stale-token"
    (tmp_path / "desired-state.json").write_text(json.dumps(stale_desired), encoding="utf-8")

    with pytest.raises(StateUpgradeError, match="recovery bytes"):
        _resume(tmp_path, desired, ledger)


def test_runtime_resume_rejects_foreign_intent_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    intent["owner_id"] = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")

    with pytest.raises(StateUpgradeError, match="intent owner"):
        _resume(tmp_path, desired, ledger)


def test_runtime_resume_rejects_wrong_write_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    intent["status"] = "planned"
    intent["completed_writes"] = []
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")

    with pytest.raises(StateUpgradeError, match="desired-write stage"):
        _resume(tmp_path, desired, ledger)


def test_runtime_resume_rejects_hash_mismatched_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    post_hashes = intent["post_hashes"]
    assert isinstance(post_hashes, dict)
    post_hashes["desired"] = "a" * 64
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")

    with pytest.raises(StateUpgradeError, match="recovery bytes"):
        _resume(tmp_path, desired, ledger)


def test_runtime_complete_intent_recovers_stale_applied_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    intent["status"] = "complete"
    intent["completed_writes"] = ["owner", "desired", "applied", "ledger"]
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")
    protected = (tmp_path / "raw-input.json").read_bytes()

    _resume(tmp_path, desired, ledger)

    completed = _intent_payload(tmp_path)
    applied = AppliedStateCheckpoint.from_dict(read_json(tmp_path / "applied-state.json"))
    current_desired = read_json(tmp_path / "desired-state.json")
    assert completed["status"] == "complete"
    generation = completed["generation"]
    assert isinstance(generation, int)
    assert generation > 1
    assert applied.desired_state_fingerprint == sha256(
        json.dumps(current_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert (tmp_path / "raw-input.json").read_bytes() == protected


def test_enabled_sync_projection_recovers_terminal_runtime_state_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired, ledger = _interrupted_runtime_projection(tmp_path, monkeypatch)
    intent = _intent_payload(tmp_path)
    intent["status"] = "complete"
    intent["completed_writes"] = ["owner", "desired", "applied", "ledger"]
    (tmp_path / _INTENT).write_text(json.dumps(intent), encoding="utf-8")
    owner_id = planned_sync_owner_id(tmp_path)
    sync_desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="b" * 64,
        litellm_image_digest=desired.runtime_images.litellm,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=ScheduleSpec.for_shared_core(
            stack_name=desired.stack_name,
            compose_id="compose-1",
            owner_id=owner_id,
        ),
    )

    result = reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=EnabledSyncBackend(
            SyncScheduleOutcome(
                sync_desired,
                AppliedSyncState.initial(sync_desired),
                None,
            )
        ),
        desired_state=desired,
        ownership_ledger=ledger,
    )

    assert result.desired_state.opencode_go_sync == sync_desired
