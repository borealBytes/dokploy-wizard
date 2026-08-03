from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
)
from dokploy_wizard.uninstall.executor import UninstallExecutionError, execute_uninstall_plan
from dokploy_wizard.uninstall.planner import PlannedDeletion, build_uninstall_plan

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


def _sync_desired(stack_name: str) -> SyncDesiredState:
    schedule = ScheduleSpec.for_shared_core(
        stack_name=stack_name,
        compose_id="compose-1",
        owner_id=_OWNER,
    )
    return SyncDesiredState.from_schedule(
        owner_id=_OWNER,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume=f"{stack_name}-shared-litellm-data",
        schedule_spec=schedule,
    )


def _resource(policy: str) -> OwnedResource:
    provenance = "created" if policy == "delete" else "reused"
    return OwnedResource(
        resource_type="shared_core_sync_schedule",
        resource_id="schedule-1",
        scope="shared-core-sync:wizard",
        metadata=SyncOwnershipMetadata(
            owner_id=_OWNER,
            action_provenance=provenance,
            remote_fingerprint="c" * 64,
            spec_hash="d" * 64,
            physical_target_id="schedule-1",
            creation_receipt_sha256="e" * 64 if policy == "delete" else None,
            preimage_receipt_sha256=None,
            deletion_policy=policy,
        ),
    )


@dataclass
class FakeUninstallBackend:
    state_dir: Path
    persist_receipt: bool = True
    disabled: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def delete(self, deletion: PlannedDeletion) -> None:
        from dokploy_wizard.dokploy.shared_core_schedule_teardown import (
            ScheduleTeardownReceipt,
        )
        from dokploy_wizard.state.sync_artifacts import SyncArtifactStore

        self.deleted.append(deletion.resource.resource_id)
        metadata = deletion.resource.metadata
        if self.persist_receipt and metadata is not None:
            SyncArtifactStore(self.state_dir).persist_schedule_teardown(
                ScheduleTeardownReceipt(
                    owner_id=metadata.owner_id,
                    action="deleted",
                    schedule_id=deletion.resource.resource_id,
                    compose_id="compose-1",
                    authorization_receipt_sha256="e" * 64,
                    before_sha256="f" * 64,
                    after_sha256=None,
                    completed_at=datetime.now(tz=UTC).isoformat(),
                )
            )

    def disable_sync_schedule(
        self,
        *,
        resource: OwnedResource,
        desired: SyncDesiredState,
        applied: AppliedSyncState,
    ) -> AppliedSyncState:
        self.disabled.append(resource.resource_id)
        return replace(applied, disable_tombstone_sha256="f" * 64)


def _seed(
    tmp_path: Path, policy: str
) -> tuple[RawEnvInput, DesiredState, OwnershipLedger]:
    raw = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    base = resolve_desired_state(raw)
    desired = replace(base, opencode_go_sync=_sync_desired(base.stack_name))
    sync_desired = desired.opencode_go_sync
    assert sync_desired is not None
    applied_sync = AppliedSyncState.initial(sync_desired)
    ledger = OwnershipLedger(format_version=1, resources=(_resource(policy),))
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=("shared_core",),
            runtime_images=desired.runtime_images,
            opencode_go_sync=applied_sync,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    (tmp_path / "shared-core-sync-owner.json").write_text("owner", encoding="utf-8")
    (tmp_path / "state-upgrade-intent-v1.json").write_text("intent", encoding="utf-8")
    return raw, desired, ledger


def test_retain_disables_schedule_and_preserves_owner_state(tmp_path: Path) -> None:
    raw, desired, ledger = _seed(tmp_path, "delete")
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=False,
    )
    backend = FakeUninstallBackend(tmp_path)

    result = execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert backend.disabled == ["schedule-1"]
    assert backend.deleted == []
    assert not result.state_cleared
    assert (tmp_path / "shared-core-sync-owner.json").exists()


def test_destroy_removes_created_schedule_then_owner_and_intent(tmp_path: Path) -> None:
    raw, desired, ledger = _seed(tmp_path, "delete")
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )
    backend = FakeUninstallBackend(tmp_path)

    result = execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert backend.deleted == ["schedule-1"]
    assert result.state_cleared
    assert not (tmp_path / "shared-core-sync-owner.json").exists()
    assert not (tmp_path / "state-upgrade-intent-v1.json").exists()


def test_destroy_preserves_unproven_schedule_without_delete_authority(tmp_path: Path) -> None:
    raw, desired, ledger = _seed(tmp_path, "preserve")

    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )

    assert plan.deletions == ()
    assert tuple(item.resource_id for item in plan.retained_resources) == ("schedule-1",)


def test_destroy_keeps_ledger_when_schedule_teardown_receipt_is_missing(
    tmp_path: Path,
) -> None:
    raw, desired, ledger = _seed(tmp_path, "delete")
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )

    with pytest.raises(UninstallExecutionError, match="receipt"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=FakeUninstallBackend(tmp_path, persist_receipt=False),
            dry_run=False,
        )

    persisted = load_state_dir(tmp_path).ownership_ledger
    assert persisted is not None
    assert persisted.resources == ledger.resources
