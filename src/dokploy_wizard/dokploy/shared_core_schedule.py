"""Fail-closed Dokploy reconciliation for the owned Shared Core sync schedule."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_ownership import (
    ownership_metadata,
    require_owner,
)
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    DisableTombstone,
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
)


@runtime_checkable
class SharedCoreScheduleClient(Protocol):
    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]: ...

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
    ) -> DokployScheduleRecord: ...

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
    ) -> DokployScheduleRecord: ...

    def delete_schedule(self, *, schedule_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class OwnedScheduleResult:
    applied: AppliedSyncState
    metadata: SyncOwnershipMetadata
    receipt: ScheduleMutationReceipt


@dataclass(frozen=True, slots=True)
class DisabledScheduleResult:
    applied: AppliedSyncState
    tombstone: DisableTombstone | None


def reconcile_owned_schedule(
    *,
    client: SharedCoreScheduleClient,
    desired: SyncDesiredState,
    applied: AppliedSyncState,
) -> AppliedSyncState:
    return reconcile_owned_schedule_contract(
        client=client,
        desired=desired,
        applied=applied,
        existing_metadata=None,
    ).applied


def reconcile_owned_schedule_contract(
    *,
    client: SharedCoreScheduleClient,
    desired: SyncDesiredState,
    applied: AppliedSyncState,
    existing_metadata: SyncOwnershipMetadata | None,
) -> OwnedScheduleResult:
    matches = _named_matches(client, desired)
    if len(matches) > 1:
        raise SyncStateError("Multiple schedules match the owned sync schedule name.")
    if not matches:
        if applied.dokploy_schedule_id is not None:
            raise SyncStateError("Previously applied sync schedule is missing.")
        _create(client, desired)
        action = "created"
        preimage_sha256 = None
        preimage = None
    else:
        existing = matches[0]
        require_owner(existing, desired.owner_id)
        if applied.dokploy_schedule_id not in {None, existing.schedule_id}:
            raise SyncStateError("Applied sync schedule id does not match the remote schedule.")
        if _matches_spec(existing, desired):
            action = "reused"
            preimage_sha256 = None
            preimage = None
        else:
            if applied.dokploy_schedule_id != existing.schedule_id:
                raise SyncStateError("Same-name schedule has a different unowned specification.")
            action = "updated"
            preimage_sha256 = schedule_record_fingerprint(existing)
            preimage = existing
            _update(client, desired, existing.schedule_id, enabled=True)
    verified = _named_matches(client, desired)
    if len(verified) != 1 or not _matches_spec(verified[0], desired):
        raise SyncStateError("Dokploy did not persist the exact owned sync schedule.")
    require_owner(verified[0], desired.owner_id)
    receipt = ScheduleMutationReceipt(
        owner_id=desired.owner_id,
        action=action,
        desired_fingerprint=desired.fingerprint(),
        before_spec_sha256=preimage_sha256,
        remote=verified[0],
        preimage=preimage,
    )
    applied_state = replace(
        applied,
        desired_fingerprint=desired.fingerprint(),
        dokploy_schedule_id=verified[0].schedule_id,
        compose_id=desired.schedule_spec.compose_id,
        last_sync_receipt_sha256=(
            receipt.sha256() if action != "reused" else applied.last_sync_receipt_sha256
        ),
        disable_tombstone_sha256=None,
    )
    return OwnedScheduleResult(
        applied=applied_state,
        metadata=ownership_metadata(desired, verified[0], receipt, existing_metadata),
        receipt=receipt,
    )


def disable_owned_schedule(
    *, client: SharedCoreScheduleClient, desired: SyncDesiredState, applied: AppliedSyncState
) -> AppliedSyncState:
    return disable_owned_schedule_contract(
        client=client,
        desired=desired,
        applied=applied,
    ).applied


def disable_owned_schedule_contract(
    *, client: SharedCoreScheduleClient, desired: SyncDesiredState, applied: AppliedSyncState
) -> DisabledScheduleResult:
    if applied.dokploy_schedule_id is None:
        return DisabledScheduleResult(applied=applied, tombstone=None)
    matches = _named_matches(client, desired)
    if len(matches) != 1 or matches[0].schedule_id != applied.dokploy_schedule_id:
        raise SyncStateError("Owned sync schedule cannot be identified for disable.")
    require_owner(matches[0], desired.owner_id)
    _update(client, desired, matches[0].schedule_id, enabled=False)
    disabled = _named_matches(client, desired)
    if len(disabled) != 1 or not _matches_spec(disabled[0], desired, enabled=False):
        raise SyncStateError("Dokploy did not persist the owned schedule disable tombstone.")
    tombstone = DisableTombstone(
        owner_id=desired.owner_id,
        schedule_id=disabled[0].schedule_id,
        desired_fingerprint=desired.fingerprint(),
        remote_fingerprint=schedule_record_fingerprint(disabled[0]),
    )
    return DisabledScheduleResult(
        applied=replace(applied, disable_tombstone_sha256=tombstone.sha256()),
        tombstone=tombstone,
    )


def _named_matches(
    client: SharedCoreScheduleClient, desired: SyncDesiredState
) -> tuple[DokployScheduleRecord, ...]:
    return tuple(
        item
        for item in client.list_compose_schedules(compose_id=desired.schedule_spec.compose_id)
        if item.name == desired.schedule_spec.name
    )


def _create(client: SharedCoreScheduleClient, desired: SyncDesiredState) -> None:
    spec = desired.schedule_spec
    client.create_schedule(
        name=spec.name,
        compose_id=spec.compose_id,
        service_name=spec.service_name,
        cron_expression=spec.cron_expression,
        timezone=spec.timezone,
        shell_type=spec.shell_type,
        command=spec.command,
        enabled=True,
    )


def _update(
    client: SharedCoreScheduleClient,
    desired: SyncDesiredState,
    schedule_id: str,
    *,
    enabled: bool,
) -> None:
    spec = desired.schedule_spec
    client.update_schedule(
        schedule_id=schedule_id,
        name=spec.name,
        compose_id=spec.compose_id,
        service_name=spec.service_name,
        cron_expression=spec.cron_expression,
        timezone=spec.timezone,
        shell_type=spec.shell_type,
        command=spec.command,
        enabled=enabled,
    )


def _matches_spec(
    record: DokployScheduleRecord,
    desired: SyncDesiredState,
    *,
    enabled: bool | None = None,
) -> bool:
    spec = desired.schedule_spec
    expected_enabled = spec.enabled if enabled is None else enabled
    return (
        record.name == spec.name
        and record.compose_id == spec.compose_id
        and record.service_name == spec.service_name
        and record.cron_expression == spec.cron_expression
        and record.timezone == spec.timezone
        and record.shell_type == spec.shell_type
        and record.command == spec.command
        and record.schedule_type == spec.schedule_type
        and record.enabled is expected_enabled
    )
