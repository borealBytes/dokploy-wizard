"""Durable process lifecycle actions for workspace catalog transactions."""

from __future__ import annotations

from collections.abc import Callable

from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    TransactionBlockedError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import (
    ProcessControl,
    start_record_processes,
    stop_record_processes,
    verify_record_processes,
)

CurrentRecord = Callable[[str], TransactionRecord]
PersistRecord = Callable[[TransactionRecord, TransactionRecord], TransactionRecord]
BlockRecord = Callable[[TransactionRecord, WorkspaceCatalogSyncError], None]
CommitRecord = Callable[[TransactionRecord], TransactionRecord]


def stop_processes(
    *,
    cas_token: str,
    current: CurrentRecord,
    persist: PersistRecord,
    block: BlockRecord,
    control: ProcessControl,
    crash_after: str | None,
) -> TransactionRecord:
    record = current(cas_token)
    if record.phase != "switched":
        raise WorkspaceCatalogSyncError("workspace transaction files are not switched")
    try:
        record = stop_record_processes(record, persist=persist, control=control)
        transitions.crash_at(crash_after, "processes_stopped")
        return record
    except WorkspaceCatalogSyncError as error:
        block(record, error)
        raise TransactionBlockedError(str(error)) from error


def start_processes(
    *,
    cas_token: str,
    current: CurrentRecord,
    persist: PersistRecord,
    block: BlockRecord,
    control: ProcessControl,
    crash_after: str | None,
) -> TransactionRecord:
    record = current(cas_token)
    if record.phase != "processes_stopped":
        raise WorkspaceCatalogSyncError("workspace transaction processes are not stopped")
    try:
        record = start_record_processes(record, persist=persist, control=control)
        transitions.crash_at(crash_after, "processes_started")
        return record
    except WorkspaceCatalogSyncError as error:
        block(record, error)
        raise TransactionBlockedError(str(error)) from error


def verify_health(
    *,
    cas_token: str,
    current: CurrentRecord,
    persist: PersistRecord,
    block: BlockRecord,
    commit: CommitRecord,
    control: ProcessControl,
    crash_after: str | None,
) -> TransactionRecord:
    record = current(cas_token)
    if record.phase != "processes_started":
        raise WorkspaceCatalogSyncError("workspace transaction processes are not started")
    try:
        record = verify_record_processes(record, persist=persist, control=control)
        transitions.crash_at(crash_after, "health_verified")
        return commit(record)
    except WorkspaceCatalogSyncError as error:
        block(record, error)
        raise TransactionBlockedError(str(error)) from error
