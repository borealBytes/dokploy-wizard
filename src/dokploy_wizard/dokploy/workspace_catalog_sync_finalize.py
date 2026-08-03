"""Commit and rollback finalization for workspace catalog transactions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from dokploy_wizard.dokploy.workspace_catalog_sync_cleanup import (
    cleanup_sensitive_files,
    rollback_file_targets,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    TransactionRecord,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import (
    ProcessControl,
    require_process_control,
    restart_rollback_processes,
    stop_started_replacements,
)

PersistRecord = Callable[[TransactionRecord, TransactionRecord], TransactionRecord]
ArtifactPath = Callable[[int], Path]


def rollback_transaction(
    record: TransactionRecord,
    *,
    control: ProcessControl | None,
    transaction_dir: Path,
    trusted_root: Path,
    persist: PersistRecord,
    preimage_path: ArtifactPath,
    staged_path: ArtifactPath,
) -> TransactionRecord:
    process_control = require_process_control(control) if record.processes else None
    if process_control is not None:
        stop_started_replacements(record, process_control)
    record = rollback_file_targets(
        record,
        persist=persist,
        preimage_path=preimage_path,
        staged_path=staged_path,
        trusted_root=trusted_root,
    )
    if process_control is not None:
        restart_rollback_processes(record, process_control)
    cleanup_sensitive_files(
        transaction_dir,
        target_count=len(record.targets),
        trusted_root=trusted_root,
    )
    return record


def commit_transaction(
    record: TransactionRecord,
    *,
    transaction_dir: Path,
    trusted_root: Path,
    persist: PersistRecord,
) -> TransactionRecord:
    committed = persist(record, replace(record, phase="committed"))
    cleanup_sensitive_files(
        transaction_dir,
        target_count=len(committed.targets),
        trusted_root=trusted_root,
    )
    return committed
