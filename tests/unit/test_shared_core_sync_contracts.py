from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule import (
    reconcile_owned_schedule,
    reconcile_owned_schedule_contract,
)
from dokploy_wizard.dokploy.shared_core_schedule_receipt import ScheduleMutationReceipt
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncStateError,
    ensure_owner,
)


class _ScheduleClient:
    def __init__(self, schedules: tuple[DokployScheduleRecord, ...] = ()) -> None:
        self.schedules = list(schedules)
        self.create_calls = 0
        self.update_calls = 0
        self.delete_calls = 0

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        del compose_id
        return tuple(self.schedules)

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        self.create_calls += 1
        record = DokployScheduleRecord(
            schedule_id=f"schedule-{self.create_calls}",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
            compose_id=compose_id,
            schedule_type="compose",
        )
        self.schedules.append(record)
        return record

    def update_schedule(
        self,
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        self.update_calls += 1
        record = DokployScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
            compose_id=compose_id,
            schedule_type="compose",
        )
        self.schedules = [
            record if item.schedule_id == schedule_id else item for item in self.schedules
        ]
        return record

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.delete_calls += 1
        self.schedules = [item for item in self.schedules if item.schedule_id != schedule_id]


def test_schedule_spec_binds_owner_command_and_every_schedule_field() -> None:
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"

    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=owner_id,
    )

    assert spec.to_dict() == {
        "command": (
            "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID=3b8e1e83-0e57-4d66-a65e-1edbf2aac838 "
            "TZ=UTC python /opt/dokploy-wizard/opencode_go_sync.py --config "
            "/opt/dokploy-wizard/opencode-go.json --state-dir "
            "/var/lib/dokploy-wizard/opencode-go --lock-file "
            "/var/lib/dokploy-wizard/opencode-go/sync.lock --once --max-runtime-seconds "
            "300 --http-timeout-seconds 30"
        ),
        "compose_id": "compose-1",
        "cron_expression": "0 3 * * *",
        "enabled": True,
        "name": "wizard-shared-litellm-opencode-go-sync",
        "schedule_type": "compose",
        "service_name": "wizard-shared-litellm",
        "shell_type": "bash",
        "timezone": "UTC",
    }
    desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=spec,
    )
    applied = AppliedSyncState.initial(desired)
    assert applied.dokploy_schedule_id is None
    assert applied.desired_fingerprint == desired.fingerprint()
    assert desired.command_sha256 == spec.command_sha256


def test_owner_reuse_rejects_different_owner_or_spec(tmp_path: Path) -> None:
    owner_path = tmp_path / "shared-core-sync-owner.json"
    first = ensure_owner(owner_path, owner_id="3b8e1e83-0e57-4d66-a65e-1edbf2aac838")

    assert first.owner_id == "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    assert ensure_owner(owner_path, owner_id=first.owner_id) == first
    with pytest.raises(SyncStateError, match="owner"):
        ensure_owner(owner_path, owner_id="99f90f71-8765-4aca-b83c-681f5ad74e80")


def test_schedule_reconcile_creates_rereads_and_binds_applied_state() -> None:
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=owner_id,
    )
    desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=spec,
    )
    client = _ScheduleClient()

    result = reconcile_owned_schedule_contract(
        client=client,
        desired=desired,
        applied=AppliedSyncState.initial(desired),
        existing_metadata=None,
    )
    applied = result.applied

    assert client.create_calls == 1
    assert client.update_calls == 0
    assert applied.dokploy_schedule_id == "schedule-1"
    assert applied.compose_id == "compose-1"
    assert applied.desired_fingerprint == desired.fingerprint()
    assert ScheduleMutationReceipt.from_dict(result.receipt.to_dict()) == result.receipt


def test_schedule_reconcile_fails_closed_for_same_name_different_owner() -> None:
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    foreign_owner_id = "99f90f71-8765-4aca-b83c-681f5ad74e80"
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=owner_id,
    )
    foreign = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=foreign_owner_id,
    )
    desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=spec,
    )
    client = _ScheduleClient(
        (
            DokployScheduleRecord(
                schedule_id="schedule-foreign",
                name=foreign.name,
                service_name=foreign.service_name,
                cron_expression=foreign.cron_expression,
                timezone=foreign.timezone,
                shell_type=foreign.shell_type,
                command=foreign.command,
                enabled=foreign.enabled,
                compose_id=foreign.compose_id,
                schedule_type=foreign.schedule_type,
            ),
        )
    )

    with pytest.raises(SyncStateError, match="owner"):
        reconcile_owned_schedule(
            client=client,
            desired=desired,
            applied=AppliedSyncState.initial(desired),
        )

    assert client.create_calls == 0
    assert client.update_calls == 0
    assert client.delete_calls == 0
