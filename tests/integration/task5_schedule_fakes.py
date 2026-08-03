from __future__ import annotations

from dataclasses import dataclass, field, replace

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
)

OWNER_ID = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
FOREIGN_OWNER_ID = "99f90f71-8765-4aca-b83c-681f5ad74e80"


@dataclass
class ScheduleClient:
    schedules: list[DokployScheduleRecord] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        self.calls.append("list")
        return tuple(item for item in self.schedules if item.compose_id == compose_id)

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
        self.calls.append("create")
        record = DokployScheduleRecord(
            schedule_id="sync-1",
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
        self.calls.append("update")
        current = next(item for item in self.schedules if item.schedule_id == schedule_id)
        updated = replace(
            current,
            name=name,
            compose_id=compose_id,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [
            updated if item.schedule_id == schedule_id else item for item in self.schedules
        ]
        return updated

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.calls.append("delete")
        self.schedules = [item for item in self.schedules if item.schedule_id != schedule_id]


def desired(owner_id: str = OWNER_ID) -> SyncDesiredState:
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=owner_id,
    )
    return SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=spec,
    )


def record(sync_desired: SyncDesiredState, *, schedule_id: str = "sync-1") -> DokployScheduleRecord:
    spec = sync_desired.schedule_spec
    return DokployScheduleRecord(
        schedule_id=schedule_id,
        name=spec.name,
        service_name=spec.service_name,
        cron_expression=spec.cron_expression,
        timezone=spec.timezone,
        shell_type=spec.shell_type,
        command=spec.command,
        enabled=spec.enabled,
        compose_id=spec.compose_id,
        schedule_type=spec.schedule_type,
    )


def applied_with_schedule(sync_desired: SyncDesiredState, schedule_id: str) -> AppliedSyncState:
    return replace(
        AppliedSyncState.initial(sync_desired),
        dokploy_schedule_id=schedule_id,
        compose_id=sync_desired.schedule_spec.compose_id,
    )
