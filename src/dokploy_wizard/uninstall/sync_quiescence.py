"""Exclusive synchronizer barrier for schedule lifecycle mutations."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator

from dokploy_wizard.state import OwnedResource
from dokploy_wizard.state.shared_core_sync import AppliedSyncState, SyncDesiredState
from dokploy_wizard.uninstall.errors import UninstallExecutionError

_QUIESCE_TIMEOUT_SECONDS: Final = 300.0
_RETRY_SECONDS: Final = 0.05


class SyncScheduleQuiescenceTimeout(UninstallExecutionError):
    """Raised when an active synchronizer does not release its exact lock in time."""


@dataclass(frozen=True, slots=True)
class SyncScheduleQuiescence:
    """Receipt-bound metadata-volume lock binding for one owned schedule."""

    metadata_root: Path
    resource: OwnedResource
    desired: SyncDesiredState
    applied: AppliedSyncState

    def __post_init__(self) -> None:
        metadata = self.resource.metadata
        if (
            metadata is None
            or metadata.owner_id != self.desired.owner_id
            or metadata.physical_target_id != self.resource.resource_id
            or self.applied.dokploy_schedule_id != self.resource.resource_id
            or self.applied.compose_id != self.desired.schedule_spec.compose_id
        ):
            raise UninstallExecutionError("Sync quiescence does not bind the owned schedule.")


@contextmanager
def quiesce_sync_schedule(
    quiescence: SyncScheduleQuiescence,
    *,
    timeout_seconds: float = _QUIESCE_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the helper lock across a schedule mutation after active sync completes."""

    if timeout_seconds <= 0:
        raise UninstallExecutionError("Sync quiescence timeout must be positive.")
    if not quiescence.metadata_root.is_dir():
        raise UninstallExecutionError("Sync metadata volume is unavailable for quiescence.")
    lock_path = quiescence.metadata_root / "sync.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _require_stable_lock_path(lock_path, descriptor)
        _acquire_lock(descriptor, timeout_seconds)
        _require_stable_lock_path(lock_path, descriptor)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _acquire_lock(descriptor: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise SyncScheduleQuiescenceTimeout(
                    "Timed out waiting for the active sync helper to quiesce."
                ) from error
            time.sleep(_RETRY_SECONDS)


def _require_stable_lock_path(lock_path: Path, descriptor: int) -> None:
    observed = os.fstat(descriptor)
    if not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise UninstallExecutionError("Sync quiescence lock has unsafe metadata.")
    current = lock_path.stat(follow_symlinks=False)
    if (observed.st_dev, observed.st_ino) != (current.st_dev, current.st_ino):
        raise UninstallExecutionError("Sync quiescence lock pathname changed during acquisition.")
