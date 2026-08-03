"""Typed backend contracts for uninstall execution."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dokploy_wizard.state import DesiredState, OwnedResource
from dokploy_wizard.state.shared_core_sync import AppliedSyncState, SyncDesiredState
from dokploy_wizard.uninstall.planner import PlannedDeletion


class UninstallBackend(Protocol):
    def delete(self, deletion: PlannedDeletion) -> None: ...


@runtime_checkable
class CoderSecretDestroyingBackend(Protocol):
    def destroy_coder_secrets(self, *, desired_state: DesiredState) -> None: ...


@runtime_checkable
class RetainedSyncScheduleDisabler(Protocol):
    def disable_sync_schedule(
        self,
        *,
        resource: OwnedResource,
        desired: SyncDesiredState,
        applied: AppliedSyncState,
    ) -> AppliedSyncState: ...
