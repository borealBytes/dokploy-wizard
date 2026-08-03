from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
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
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.uninstall import shell_backend, sync_schedule
from dokploy_wizard.uninstall.executor import (
    ShellUninstallBackend,
    UninstallExecutionError,
    execute_uninstall_plan,
)
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass
class ScheduleApi:
    current: list[DokployScheduleRecord]
    delete_calls: int = 0

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        assert compose_id == "compose-1"
        return tuple(self.current)

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.delete_calls += 1
        self.current = [item for item in self.current if item.schedule_id != schedule_id]

    def update_schedule(self, **_: str | bool) -> DokployScheduleRecord:
        raise AssertionError("schedule restoration is outside this created-schedule test")


@dataclass(frozen=True, slots=True)
class VolumeRuntime:
    root: Path

    def volume_mountpoint(self, volume: str) -> Path:
        assert volume == "wizard-shared-litellm-data"
        return self.root


def _desired() -> SyncDesiredState:
    return SyncDesiredState.from_schedule(
        owner_id=_OWNER,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=ScheduleSpec.for_shared_core(
            stack_name="wizard",
            compose_id="compose-1",
            owner_id=_OWNER,
        ),
    )


def _record(desired: SyncDesiredState, *, command: str | None = None) -> DokployScheduleRecord:
    spec = desired.schedule_spec
    return DokployScheduleRecord(
        schedule_id="schedule-1",
        name=spec.name,
        service_name=spec.service_name,
        cron_expression=spec.cron_expression,
        timezone=spec.timezone,
        shell_type=spec.shell_type,
        command=spec.command if command is None else command,
        enabled=True,
        compose_id=spec.compose_id,
        schedule_type=spec.schedule_type,
    )


def _deletion(state_dir: Path, record: DokployScheduleRecord) -> PlannedDeletion:
    desired = _desired()
    receipt = ScheduleMutationReceipt(
        owner_id=desired.owner_id,
        action="created",
        desired_fingerprint=desired.fingerprint(),
        before_spec_sha256=None,
        remote=record,
    )
    receipt_digest = SyncArtifactStore(state_dir).persist_schedule_receipt(receipt)
    return PlannedDeletion(
        resource=OwnedResource(
            resource_type="shared_core_sync_schedule",
            resource_id=record.schedule_id,
            scope="shared-core-sync:wizard",
            metadata=SyncOwnershipMetadata(
                owner_id=desired.owner_id,
                action_provenance="created",
                remote_fingerprint=schedule_record_fingerprint(record),
                spec_hash=desired.schedule_spec_sha256,
                physical_target_id=record.schedule_id,
                creation_receipt_sha256=receipt_digest,
                preimage_receipt_sha256=None,
                deletion_policy="delete",
            ),
        ),
        phase="shared_core",
        policy="destroy_only",
    )


def test_concurrent_fingerprint_mismatch_blocks_schedule_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired = _desired()
    receipt_record = _record(desired)
    deletion = _deletion(tmp_path, receipt_record)
    api = ScheduleApi([_record(desired, command=f"{receipt_record.command} --drift")])
    monkeypatch.setattr(sync_schedule, "DokployApiClient", lambda **_: api)
    backend = ShellUninstallBackend(
        RawEnvInput(
            format_version=1,
            values={"DOKPLOY_API_URL": "https://dokploy.example.test", "DOKPLOY_API_KEY": "key"},
        ),
        state_dir=tmp_path,
    )

    with pytest.raises(UninstallExecutionError, match="fingerprint"):
        backend._sync_teardown().delete(deletion.resource)

    assert api.delete_calls == 0


def test_full_uninstall_quiesces_then_deletes_after_provenance_migration_all_resource_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_desired = _desired()
    record = _record(sync_desired)
    deletion = _deletion(tmp_path, record)
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired_state = replace(resolve_desired_state(raw), opencode_go_sync=sync_desired)
    sync_applied = replace(
        AppliedSyncState.initial(sync_desired),
        dokploy_schedule_id=record.schedule_id,
        compose_id=record.compose_id,
    )
    write_target_state(tmp_path, raw, desired_state)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=("shared_core",),
            runtime_images=desired_state.runtime_images,
            opencode_go_sync=sync_applied,
        ),
    )
    metadata_root = tmp_path / "detached-metadata"
    metadata_root.mkdir()
    api = ScheduleApi([record])
    monkeypatch.setattr(sync_schedule, "DokployApiClient", lambda **_: api)
    monkeypatch.setattr(
        shell_backend,
        "SubprocessDockerHelperRuntime",
        lambda: VolumeRuntime(metadata_root),
    )
    backend = ShellUninstallBackend(
        RawEnvInput(
            format_version=1,
            values={"DOKPLOY_API_URL": "https://dokploy.example.test", "DOKPLOY_API_KEY": "key"},
        ),
        state_dir=tmp_path,
    )

    backend.delete(deletion)

    teardown = SyncArtifactStore(tmp_path).load_schedule_teardown(record.schedule_id)
    assert api.current == []
    assert api.delete_calls == 1
    assert (metadata_root / "sync.lock").is_file()
    assert teardown is not None
    assert teardown.action == "deleted"


def test_default_backend_preserves_ledger_when_cloudflare_authority_is_missing(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired = resolve_desired_state(raw)
    resource = OwnedResource(
        resource_type="cloudflare_tunnel",
        resource_id="tunnel-1",
        scope="account:account-123",
    )
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    deletion = PlannedDeletion(resource=resource, phase="networking", policy="retain_safe")
    plan = UninstallPlan(
        mode="retain",
        environment=desired.stack_name,
        deletions=(deletion,),
        retained_resources=(),
        warnings=(),
    )
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=("preflight",),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)

    with pytest.raises(UninstallExecutionError, match="recorded uninstall authority"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=ShellUninstallBackend(raw, state_dir=tmp_path),
            dry_run=False,
        )

    loaded = load_state_dir(tmp_path)
    assert loaded.ownership_ledger == ledger
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == ("preflight",)
