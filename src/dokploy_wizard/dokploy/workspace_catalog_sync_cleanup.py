"""Private transaction cleanup and adoption receipt publication."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from dokploy_wizard.dokploy import workspace_catalog_sync_transition as _transitions
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    LegacyAdoptionReceipt,
    TransactionBlockedError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import authorized_parent
from dokploy_wizard.dokploy.workspace_catalog_sync_publish import create_bytes
from dokploy_wizard.dokploy.workspace_catalog_sync_targets import (
    matches_preimage,
    restore_preimage,
    verify_staged_target,
    write_staged_target,
)


def cleanup_sensitive_files(
    transaction_dir: Path, *, target_count: int, trusted_root: Path
) -> None:
    """Securely unlink only mode-0600 staged and byte-preimage files."""
    if target_count < 1:
        raise WorkspaceCatalogSyncError("workspace transaction target count is invalid")
    for name in ("preimages", "staged"):
        _cleanup_directory(
            transaction_dir / name, target_count, trusted_root=trusted_root
        )


def advance_file_targets(
    record: TransactionRecord,
    *,
    persist: Callable[[TransactionRecord, TransactionRecord], TransactionRecord],
    staged_path: Callable[[int], Path],
    crash_after: str | None,
    trusted_root: Path,
) -> TransactionRecord:
    """Advance durable target phases through the file switch without process actions."""
    if record.phase == "prepared":
        for receipt in record.targets:
            from dokploy_wizard.dokploy.workspace_catalog_sync_io import verify_snapshot

            verify_snapshot(receipt, trusted_root=trusted_root)
        for index, receipt in enumerate(record.targets):
            record = persist(record, _transitions.target_status(record, index, "write_intent"))
            write_staged_target(
                receipt,
                staged_path=staged_path(index),
                trusted_root=trusted_root,
            )
            written = _transitions.target_status(record, index, "written", post=True)
            record = persist(record, written)
        record = persist(record, replace(record, phase="files_written"))
        _transitions.crash_at(crash_after, "files_written")
    if record.phase == "files_written":
        for index, receipt in enumerate(record.targets):
            verify_staged_target(
                receipt,
                staged_path=staged_path(index),
                trusted_root=trusted_root,
            )
        record = persist(record, replace(record, phase="pre_switch_verified"))
        _transitions.crash_at(crash_after, "pre_switch_verified")
    if record.phase == "pre_switch_verified":
        record = persist(record, replace(record, phase="switched"))
        _transitions.crash_at(crash_after, "switched")
    return record


def rollback_file_targets(
    record: TransactionRecord,
    *,
    persist: Callable[[TransactionRecord, TransactionRecord], TransactionRecord],
    preimage_path: Callable[[int], Path],
    staged_path: Callable[[int], Path],
    trusted_root: Path,
) -> TransactionRecord:
    """Restore only preimage-or-stage verified targets and persist rolled-back state."""
    if record.phase != "rollback_started":
        record = persist(record, replace(record, phase="rollback_started"))
    for index, receipt in enumerate(record.targets):
        if receipt.status == "rolled_back":
            continue
        if matches_preimage(receipt, trusted_root=trusted_root):
            record = persist(
                record, _transitions.target_status(record, index, "rolled_back")
            )
            continue
        current = verify_staged_target(
            receipt,
            staged_path=staged_path(index),
            trusted_root=trusted_root,
        )
        if receipt.status != "rollback_intent":
            record = persist(record, _transitions.target_status(record, index, "rollback_intent"))
        restore_preimage(
            receipt,
            preimage_path=preimage_path(index),
            trusted_root=trusted_root,
            expected=current,
        )
        record = persist(record, _transitions.target_status(record, index, "rolled_back"))
    return persist(record, replace(record, phase="rolled_back"))


def verify_resolution_targets(
    record: TransactionRecord,
    *,
    accept_preimage: bool,
    staged_path: Callable[[int], Path],
    trusted_root: Path,
) -> None:
    """Require a user-resolved target to equal an accepted durable snapshot."""
    for index, receipt in enumerate(record.targets):
        if not (
            accept_preimage and matches_preimage(receipt, trusted_root=trusted_root)
        ):
            verify_staged_target(
                receipt,
                staged_path=staged_path(index),
                trusted_root=trusted_root,
            )


def _cleanup_directory(
    directory: Path, target_count: int, *, trusted_root: Path
) -> None:
    with authorized_parent(directory, trusted_root=trusted_root) as authority:
        try:
            descriptor = os.open(
                authority.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=authority.descriptor,
            )
        except OSError as error:
            raise TransactionBlockedError(
                "workspace transaction sensitive directory is invalid"
            ) from error
        try:
            for index in range(target_count):
                filename = f"{index}.bin"
                try:
                    metadata = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise WorkspaceCatalogSyncError(
                        "workspace transaction sensitive file is invalid"
                    )
                os.unlink(filename, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_adoption(
    transaction_dir: Path,
    receipt: LegacyAdoptionReceipt,
    *,
    trusted_root: Path,
) -> None:
    path = transaction_dir / "legacy-adoption-v1.json"
    content = (json.dumps(asdict(receipt), sort_keys=True) + "\n").encode()
    create_bytes(path, content, 0o600, trusted_root=trusted_root)
