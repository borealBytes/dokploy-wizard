"""Exact durable lease-receipt binding and dead-helper recovery."""

from __future__ import annotations

import fcntl
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from dokploy_wizard.dokploy.sync_helper_identity import (
    ParentIdentity,
    verify_parent_identity,
)
from dokploy_wizard.dokploy.sync_helper_lease import lease_is_fresh
from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.state.sync_schema import SyncStateError
from dokploy_wizard.state.upgrade_io import atomic_json, read_json

_RECOVERABLE_PHASES = frozenset(
    {
        "starting",
        "acquired_held",
        "parent_running",
        "after_snapshot_written",
        "release_requested",
    }
)


def bind_or_recover_lease_receipt(
    *,
    path: Path,
    request: LeaseRequest,
    container_id: str,
    lock_path: Path,
) -> LeaseReceipt:
    """Bind the full ID or recover one stale same-lease receipt after OS unlock."""

    receipt = LeaseReceipt.from_dict(read_json(path, json_values=True))
    expected = LeaseReceipt.created(request=request, container_id=None)
    if receipt == expected:
        bound = replace(
            receipt,
            container_id=container_id,
            generation=receipt.generation + 1,
            receipt_version=receipt.receipt_version + 1,
        )
        atomic_json(path, bound.to_dict())
        return bound
    if receipt.phase == "created" and receipt.container_id == container_id:
        return receipt
    if receipt.phase not in _RECOVERABLE_PHASES or receipt.container_id != container_id:
        raise SyncStateError("Existing helper receipt cannot recover this exact lease.")
    if lease_is_fresh(receipt, datetime.now(tz=UTC)):
        raise SyncStateError("Existing helper lease is still fresh.")
    verify_parent_identity(
        ParentIdentity(
            pid=request.parent_pid,
            start_time_ticks=request.parent_start_time_ticks,
            argv_sha256=request.parent_argv_sha256,
        )
    )
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SyncStateError("Stale helper receipt still owns the OS lock.") from error
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    recovered = replace(
        receipt,
        generation=receipt.generation + 1,
        receipt_version=receipt.receipt_version + 1,
        phase="created",
        heartbeat_at=None,
        heartbeat_deadline_at=None,
        lock_inode=None,
        result_sha256=None,
        error=None,
    )
    atomic_json(path, recovered.to_dict())
    return recovered
