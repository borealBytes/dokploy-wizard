from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.lifecycle.changes import (
    applicable_phases_for,
    classify_install_request,
)
from dokploy_wizard.lifecycle.shared_core_sync import reconcile_and_persist_sync_projection
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncOwnershipMetadata,
)
from dokploy_wizard.state.upgrade_io import atomic_json

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"


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


def test_disabled_legacy_projection_persists_images_and_second_pass_is_noop(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    legacy_desired = desired.to_dict()
    legacy_desired.pop("runtime_images")
    legacy_desired["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (tmp_path / "desired-state.json").write_text(
        json.dumps(legacy_desired),
        encoding="utf-8",
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
    backend = DisabledSyncBackend()

    first = reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=backend,
        desired_state=desired,
        ownership_ledger=ledger,
    )
    intent_after_first = (tmp_path / "state-upgrade-intent-v1.json").read_bytes()
    second = reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=backend,
        desired_state=desired,
        ownership_ledger=first.ownership_ledger,
    )

    loaded = load_state_dir(tmp_path)
    assert loaded.desired_state is not None
    assert loaded.applied_state is not None
    assert loaded.desired_state.runtime_images == desired.runtime_images
    assert loaded.applied_state.runtime_images == desired.runtime_images
    assert loaded.applied_state.desired_state_fingerprint == desired.fingerprint()
    assert second.desired_state == loaded.desired_state
    assert (tmp_path / "state-upgrade-intent-v1.json").read_bytes() == intent_after_first
    assert loaded.raw_input is not None
    assert loaded.ownership_ledger is not None
    rerun = classify_install_request(
        existing_raw=loaded.raw_input,
        existing_desired=loaded.desired_state,
        existing_applied=loaded.applied_state,
        requested_raw=raw,
        requested_desired=desired,
    )
    assert rerun.mode == "noop"


def test_disabled_legacy_projection_sanitizes_openclaw_gateway_token(
    tmp_path: Path,
) -> None:
    # Given
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    legacy_desired = desired.to_dict()
    legacy_desired["openclaw_gateway_token"] = "legacy-openclaw-token"
    legacy_desired["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (tmp_path / "desired-state.json").write_text(
        json.dumps(legacy_desired),
        encoding="utf-8",
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

    # When
    result = reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=DisabledSyncBackend(),
        desired_state=desired,
        ownership_ledger=ledger,
    )

    # Then
    assert result.desired_state.openclaw_gateway_token is None
    assert load_state_dir(tmp_path).desired_state == result.desired_state


def test_disabled_legacy_projection_resumes_after_desired_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    legacy_desired = desired.to_dict()
    legacy_desired["openclaw_gateway_token"] = "legacy-openclaw-token"
    legacy_desired["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (tmp_path / "desired-state.json").write_text(
        json.dumps(legacy_desired),
        encoding="utf-8",
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
    interrupted = False

    def interrupt_applied_write(path: Path, payload: dict[str, object]) -> None:
        nonlocal interrupted
        if path.name == "applied-state.json" and not interrupted:
            interrupted = True
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

    # When
    result = reconcile_and_persist_sync_projection(
        state_dir=tmp_path,
        backend=DisabledSyncBackend(),
        desired_state=desired,
        ownership_ledger=ledger,
    )

    # Then
    assert result.desired_state.openclaw_gateway_token is None
    assert load_state_dir(tmp_path).desired_state == result.desired_state
