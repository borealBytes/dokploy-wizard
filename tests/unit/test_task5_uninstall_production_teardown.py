from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.state import OwnedResource, RawEnvInput
from dokploy_wizard.state.shared_core_sync import SyncOwnershipMetadata
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.uninstall import shell_backend, sync_schedule
from dokploy_wizard.uninstall.executor import ShellUninstallBackend, UninstallExecutionError
from dokploy_wizard.uninstall.planner import PlannedDeletion

_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_OWNED_COMMAND = f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER} sync-owned"


def _record(*, command: str) -> DokployScheduleRecord:
    return DokployScheduleRecord(
        schedule_id="schedule-1",
        name="wizard-opencode-go-sync",
        service_name="litellm",
        cron_expression="*/5 * * * *",
        timezone="UTC",
        shell_type="bash",
        command=command,
        enabled=True,
        compose_id="compose-1",
        schedule_type="service",
    )


@dataclass
class ScheduleApi:
    current: list[DokployScheduleRecord]
    deleted: list[str] = field(default_factory=list)
    updates: int = 0

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        assert compose_id == "compose-1"
        return tuple(self.current)

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.deleted.append(schedule_id)
        self.current = [item for item in self.current if item.schedule_id != schedule_id]

    def update_schedule(self, **values: object) -> DokployScheduleRecord:
        self.updates += 1
        restored = DokployScheduleRecord(
            schedule_id=str(values["schedule_id"]),
            name=str(values["name"]),
            service_name=str(values["service_name"]),
            cron_expression=str(values["cron_expression"]),
            timezone=str(values["timezone"]),
            shell_type=str(values["shell_type"]),
            command=str(values["command"]),
            enabled=bool(values["enabled"]),
            compose_id=str(values["compose_id"]),
            schedule_type="service",
        )
        self.current = [restored]
        return restored


def _backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: ScheduleApi,
) -> ShellUninstallBackend:
    monkeypatch.setattr(sync_schedule, "DokployApiClient", lambda **_: api)
    monkeypatch.setattr(ShellUninstallBackend, "_quiescence_from_state", lambda *_: None)
    monkeypatch.setattr(shell_backend, "quiesce_sync_schedule", lambda _: nullcontext())
    return ShellUninstallBackend(
        RawEnvInput(
            format_version=1,
            values={
                "DOKPLOY_API_URL": "https://dokploy.example.com/api",
                "DOKPLOY_API_KEY": "secret",
            }
        ),
        state_dir=tmp_path,
    )


def _deletion(
    tmp_path: Path,
    *,
    action: str,
    remote: DokployScheduleRecord,
    preimage: DokployScheduleRecord | None,
) -> PlannedDeletion:
    receipt = ScheduleMutationReceipt(
        owner_id=_OWNER,
        action=action,
        desired_fingerprint="a" * 64,
        before_spec_sha256=(
            None if preimage is None else schedule_record_fingerprint(preimage)
        ),
        remote=remote,
        preimage=preimage,
    )
    digest = SyncArtifactStore(tmp_path).persist_schedule_receipt(receipt)
    policy = "delete" if action == "created" else "restore"
    return PlannedDeletion(
        resource=OwnedResource(
            resource_type="shared_core_sync_schedule",
            resource_id=remote.schedule_id,
            scope="shared-core-sync:wizard",
            metadata=SyncOwnershipMetadata(
                owner_id=_OWNER,
                action_provenance=action,
                remote_fingerprint=schedule_record_fingerprint(remote),
                spec_hash="b" * 64,
                physical_target_id=remote.schedule_id,
                creation_receipt_sha256=digest if policy == "delete" else None,
                preimage_receipt_sha256=digest if policy == "restore" else None,
                deletion_policy=policy,
            ),
        ),
        phase="shared_core",
        policy="destroy",
    )


def test_shell_backend_delete_rereads_absence_and_resumes_from_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _record(command=_OWNED_COMMAND)
    deletion = _deletion(tmp_path, action="created", remote=remote, preimage=None)
    api = ScheduleApi([remote])
    backend = _backend(tmp_path, monkeypatch, api)

    backend.delete(deletion)
    backend.delete(deletion)

    receipt = SyncArtifactStore(tmp_path).load_schedule_teardown("schedule-1")
    assert receipt is not None
    assert receipt.action == "deleted"
    assert receipt.after_sha256 is None
    assert api.deleted == ["schedule-1"]


def test_shell_backend_restore_rereads_exact_preimage_and_resumes_from_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preimage = _record(command="operator-command")
    remote = _record(command=_OWNED_COMMAND)
    deletion = _deletion(
        tmp_path,
        action="updated",
        remote=remote,
        preimage=preimage,
    )
    api = ScheduleApi([remote])
    backend = _backend(tmp_path, monkeypatch, api)

    backend.delete(deletion)
    backend.delete(deletion)

    receipt = SyncArtifactStore(tmp_path).load_schedule_teardown("schedule-1")
    assert receipt is not None
    assert receipt.action == "restored"
    assert receipt.after_sha256 == schedule_record_fingerprint(preimage)
    assert api.current == [preimage]
    assert api.updates == 1


def test_concurrent_fingerprint_mismatch_blocks_schedule_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _record(command=_OWNED_COMMAND)
    deletion = _deletion(tmp_path, action="created", remote=remote, preimage=None)
    api = ScheduleApi([_record(command=f"{_OWNED_COMMAND} --concurrent-drift")])
    backend = _backend(tmp_path, monkeypatch, api)

    with pytest.raises(UninstallExecutionError, match="fingerprint"):
        backend.delete(deletion)

    assert api.deleted == []
