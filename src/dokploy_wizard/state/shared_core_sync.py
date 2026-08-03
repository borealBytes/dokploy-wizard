"""Public Shared Core synchronizer state interface and durable owner storage."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from dokploy_wizard.state.sync_ownership import SyncOwnershipMetadata
from dokploy_wizard.state.sync_schema import (
    JsonValue,
    ScheduleSpec,
    SyncOwner,
    SyncStateError,
)
from dokploy_wizard.state.sync_state import AppliedSyncState, SyncDesiredState

SYNC_SCHEDULE_RESOURCE_TYPE = "shared_core_sync_schedule"

__all__ = [
    "AppliedSyncState",
    "ScheduleSpec",
    "SyncDesiredState",
    "SyncOwner",
    "SyncOwnershipMetadata",
    "SyncStateError",
    "SYNC_SCHEDULE_RESOURCE_TYPE",
    "ensure_owner",
]


def ensure_owner(path: Path, *, owner_id: str | None = None) -> SyncOwner:
    """Create one mode-0600 UUID4 owner document or validate its exact reuse."""

    if path.exists():
        existing = _read_owner(path)
        if owner_id is not None and existing.owner_id != owner_id:
            raise SyncStateError("Existing schedule owner does not match the requested owner.")
        return existing
    owner = SyncOwner(owner_id=owner_id or str(uuid.uuid4()))
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, owner.to_dict())
    return owner


def _read_owner(path: Path) -> SyncOwner:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SyncStateError("Existing owner document is malformed.") from error
    if not isinstance(loaded, dict):
        raise SyncStateError("Existing owner document is malformed.")
    return SyncOwner.from_dict(loaded)


def _atomic_write(path: Path, payload: dict[str, JsonValue]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
