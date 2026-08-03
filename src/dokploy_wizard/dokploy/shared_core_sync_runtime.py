"""Production orchestration for the owned Shared Core sync schedule."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from dokploy_wizard.dokploy.shared_core_schedule import (
    SharedCoreScheduleClient,
    disable_owned_schedule_contract,
    reconcile_owned_schedule_contract,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
    ensure_owner,
)
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore


@dataclass(frozen=True, slots=True)
class SyncScheduleContext:
    stack_name: str
    compose_id: str
    state_dir: Path
    config_sha256: str
    litellm_image_digest: str
    metadata_volume: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SyncScheduleOutcome:
    desired: SyncDesiredState
    applied: AppliedSyncState
    metadata: SyncOwnershipMetadata | None


def reconcile_sync_schedule(
    client: SharedCoreScheduleClient,
    context: SyncScheduleContext,
    *,
    existing_applied: AppliedSyncState | None,
    existing_metadata: SyncOwnershipMetadata | None,
) -> SyncScheduleOutcome:
    """Reconcile or disable the schedule and persist its authoritative document."""

    owner = ensure_owner(context.state_dir / "shared-core-sync-owner.json")
    schedule = ScheduleSpec.for_shared_core(
        stack_name=context.stack_name,
        compose_id=context.compose_id,
        owner_id=owner.owner_id,
    )
    desired = SyncDesiredState.from_schedule(
        owner_id=owner.owner_id,
        config_sha256=context.config_sha256,
        litellm_image_digest=context.litellm_image_digest,
        metadata_volume=context.metadata_volume,
        schedule_spec=schedule,
    )
    if not context.enabled:
        desired = replace(desired, enabled=False)
    applied = existing_applied or AppliedSyncState.initial(desired)
    artifacts = SyncArtifactStore(context.state_dir)
    if not context.enabled:
        disabled = disable_owned_schedule_contract(
            client=client,
            desired=desired,
            applied=applied,
        )
        if disabled.tombstone is not None:
            digest = artifacts.persist_disable_tombstone(disabled.tombstone)
            applied = replace(
                disabled.applied,
                desired_fingerprint=desired.fingerprint(),
                disable_tombstone_sha256=digest,
            )
        else:
            applied = replace(disabled.applied, desired_fingerprint=desired.fingerprint())
        return SyncScheduleOutcome(desired, applied, existing_metadata)
    reconciled = reconcile_owned_schedule_contract(
        client=client,
        desired=desired,
        applied=applied,
        existing_metadata=existing_metadata,
    )
    receipt_sha256 = artifacts.persist_schedule_receipt(reconciled.receipt)
    return SyncScheduleOutcome(
        desired=desired,
        applied=replace(reconciled.applied, last_sync_receipt_sha256=receipt_sha256),
        metadata=reconciled.metadata,
    )
