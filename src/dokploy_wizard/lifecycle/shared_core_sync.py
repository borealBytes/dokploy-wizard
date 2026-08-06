"""Lifecycle projection of Shared Core sync schedule state and ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    load_state_dir,
)
from dokploy_wizard.state.shared_core_sync import (
    SYNC_SCHEDULE_RESOURCE_TYPE,
    AppliedSyncState,
    SyncOwnershipMetadata,
    SyncStateError,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.upgrade import (
    planned_sync_owner_id,
    state_upgrade_paths,
    upgrade_state_contract,
)
from dokploy_wizard.state.upgrade_io import read_json


@runtime_checkable
class SharedCoreSyncBackend(Protocol):
    def reconcile_sync_schedule(
        self,
        *,
        existing_applied: AppliedSyncState | None,
        existing_metadata: SyncOwnershipMetadata | None,
    ) -> SyncScheduleOutcome | None: ...


@runtime_checkable
class UninstallAuthorityPublisher(Protocol):
    def record_created_uninstall_authorities(
        self,
        authority_store: UninstallAuthorityStore,
        resources: tuple[OwnedResource, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SyncProjection:
    desired_state: DesiredState
    ownership_ledger: OwnershipLedger


def prepare_sync_state_upgrade(state_dir: Path) -> None:
    """Durably establish the owner-first upgrade intent before remote projection."""

    owner_id = planned_sync_owner_id(state_dir)
    upgrade_state_contract(
        state_dir=state_dir,
        owner_id=owner_id,
        paths=state_upgrade_paths(state_dir),
    )


def reconcile_and_persist_sync_projection(
    *,
    state_dir: Path,
    backend: SharedCoreSyncBackend,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
) -> SyncProjection:
    """Invoke production schedule reconciliation and persist its canonical projection."""

    loaded = load_state_dir(state_dir)
    existing = loaded.applied_state
    paths = state_upgrade_paths(state_dir)
    if existing is not None:
        desired_payload = read_json(paths["desired"])
        desired_fingerprint = sha256(
            json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if existing.desired_state_fingerprint != desired_fingerprint:
            upgrade_state_contract(
                state_dir=state_dir,
                owner_id=planned_sync_owner_id(state_dir),
                paths=paths,
                runtime_images=desired_state.runtime_images,
                ownership_ledger=ownership_ledger,
            )
            loaded = load_state_dir(state_dir)
    existing = loaded.applied_state
    metadata = _existing_metadata(ownership_ledger)
    outcome = backend.reconcile_sync_schedule(
        existing_applied=None if existing is None else existing.opencode_go_sync,
        existing_metadata=metadata,
    )
    if outcome is None:
        upgrade_state_contract(
            state_dir=state_dir,
            owner_id=planned_sync_owner_id(state_dir),
            paths=paths,
            runtime_images=desired_state.runtime_images,
            ownership_ledger=ownership_ledger,
        )
        loaded = load_state_dir(state_dir)
        if loaded.ownership_ledger is None:
            raise SyncStateError("Shared Core ownership ledger was not persisted.")
        return SyncProjection(desired_state, loaded.ownership_ledger)
    resources = tuple(
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type != SYNC_SCHEDULE_RESOURCE_TYPE
    )
    if outcome.metadata is not None:
        resources += (
            OwnedResource(
                resource_type=SYNC_SCHEDULE_RESOURCE_TYPE,
                resource_id=outcome.metadata.physical_target_id,
                scope=f"shared-core-sync:{desired_state.stack_name}",
                metadata=outcome.metadata,
            ),
        )
    projected_ledger = OwnershipLedger(
        format_version=ownership_ledger.format_version,
        resources=resources,
    )
    upgrade_state_contract(
        state_dir=state_dir,
        owner_id=outcome.desired.owner_id,
        paths=state_upgrade_paths(state_dir),
        sync_desired=outcome.desired,
        sync_applied=outcome.applied,
        ownership_ledger=projected_ledger,
    )
    loaded = load_state_dir(state_dir)
    if loaded.desired_state is None or loaded.ownership_ledger is None:
        raise SyncStateError("Shared Core sync projection was not persisted completely.")
    return SyncProjection(loaded.desired_state, loaded.ownership_ledger)


def _existing_metadata(ledger: OwnershipLedger) -> SyncOwnershipMetadata | None:
    matches = tuple(
        resource.metadata
        for resource in ledger.resources
        if resource.resource_type == SYNC_SCHEDULE_RESOURCE_TYPE
    )
    if len(matches) > 1:
        raise SyncStateError("Shared Core sync ownership ledger is ambiguous.")
    return None if not matches else matches[0]
