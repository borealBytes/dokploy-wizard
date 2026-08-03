"""Fsynced Task 5 teardown ordering barriers."""

from __future__ import annotations

import os
from pathlib import Path

from dokploy_wizard.state import OwnedResource
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.uninstall.errors import UninstallExecutionError


def clear_sync_control_documents(state_dir: Path) -> None:
    for name in ("shared-core-sync-owner.json", "state-upgrade-intent-v1.json"):
        (state_dir / name).unlink(missing_ok=True)
    descriptor = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_sync_teardown_receipt(state_dir: Path, resource: OwnedResource) -> None:
    metadata = resource.metadata
    receipt = SyncArtifactStore(state_dir).load_schedule_teardown(resource.resource_id)
    if metadata is None or receipt is None:
        raise UninstallExecutionError(
            "Sync schedule teardown receipt must be durable before ledger removal."
        )
    expected_action = "deleted" if metadata.deletion_policy == "delete" else "restored"
    if (
        receipt.owner_id != metadata.owner_id
        or receipt.schedule_id != resource.resource_id
        or receipt.action != expected_action
    ):
        raise UninstallExecutionError("Sync schedule teardown receipt does not match ownership.")
