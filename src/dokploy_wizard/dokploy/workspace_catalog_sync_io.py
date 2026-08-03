"""Durable, private storage for workspace catalog transaction records."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Final

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    TargetReceipt,
    TransactionBlockedError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import (
    authorized_parent,
    ensure_authorized_directory,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import (
    read_regular as _read_authorized_regular,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import (
    target_snapshot as _target_snapshot,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_publish import (
    atomic_bytes as _atomic_bytes,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_publish import (
    atomic_symlink as _atomic_symlink,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_record_lock import RecordLock
from dokploy_wizard.dokploy.workspace_catalog_sync_schema import (
    parse_transaction_record,
    require_mapping,
    validate_transaction_record,
)

_MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024


def sha256(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of bytes."""
    return hashlib.sha256(value).hexdigest()


def ensure_private_directory(path: Path, *, trusted_root: Path) -> None:
    """Create a mode-0700 directory tree without traversing private symlinks."""
    ensure_authorized_directory(path, trusted_root=trusted_root, private=True)


def atomic_bytes(
    path: Path,
    content: bytes,
    mode: int,
    *,
    trusted_root: Path | None = None,
    expected: TargetReceipt | None = None,
) -> None:
    """Atomically publish a mode-constrained file and remove failed temporary bytes."""
    _atomic_bytes(
        path,
        content,
        mode,
        trusted_root=trusted_root if trusted_root is not None else path.parent,
        expected=expected,
    )


def atomic_symlink(
    path: Path,
    target: str,
    *,
    trusted_root: Path | None = None,
    expected: TargetReceipt | None = None,
) -> None:
    """Atomically publish one symlink without retaining a failed temporary link."""
    _atomic_symlink(
        path,
        target,
        trusted_root=trusted_root if trusted_root is not None else path.parent,
        expected=expected,
    )


def target_snapshot(path: Path, *, trusted_root: Path | None = None) -> TargetReceipt:
    """Capture an absent, regular-file, or symlink target without dereferencing it."""
    return _target_snapshot(
        path,
        trusted_root=trusted_root if trusted_root is not None else path.parent,
    )


def verify_snapshot(receipt: TargetReceipt, *, trusted_root: Path | None = None) -> None:
    """Require a target still equals its prepared preimage snapshot."""
    current = target_snapshot(Path(receipt.path), trusted_root=trusted_root)
    expected = (receipt.pre_state, receipt.pre_sha256, receipt.pre_mode, receipt.pre_target)
    actual = (current.pre_state, current.pre_sha256, current.pre_mode, current.pre_target)
    if actual != expected:
        raise TransactionBlockedError("workspace target changed outside its transaction")


def create_record(
    path: Path, record: TransactionRecord, *, trusted_root: Path | None = None
) -> None:
    """Create the first durable record once for a transaction generation."""
    root = trusted_root if trusted_root is not None else path.parent
    with RecordLock(path, root):
        with authorized_parent(path, trusted_root=root) as authority:
            try:
                os.stat(authority.name, dir_fd=authority.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
        if exists:
            raise WorkspaceCatalogSyncError("workspace transaction generation already exists")
        _write_record(path, record, trusted_root=root)


def write_record_cas(
    path: Path,
    *,
    expected: TransactionRecord,
    record: TransactionRecord,
    trusted_root: Path | None = None,
) -> TransactionRecord:
    """Publish a legal successor from the exact locked predecessor."""
    root = trusted_root if trusted_root is not None else path.parent
    with RecordLock(path, root):
        current = read_record(path, trusted_root=root)
        return _publish_record(path, current, expected, record, trusted_root=root)


def persist_record(
    path: Path,
    expected: TransactionRecord,
    record: TransactionRecord,
    *,
    trusted_root: Path | None = None,
) -> TransactionRecord:
    """CAS-persist a proposed successor from its exact predecessor."""
    return write_record_cas(
        path, expected=expected, record=record, trusted_root=trusted_root
    )


def block_record(
    path: Path,
    record: TransactionRecord,
    reason: str,
    *,
    trusted_root: Path | None = None,
) -> TransactionRecord:
    """Persist a blocked terminal decision from the latest valid CAS record."""
    del record
    root = trusted_root if trusted_root is not None else path.parent
    with RecordLock(path, root):
        current = read_record(path, trusted_root=root)
        proposed = replace(current, phase="blocked", error=reason)
        return _publish_record(
            path, current, current, proposed, trusted_root=root
        )


def read_record(path: Path, *, trusted_root: Path | None = None) -> TransactionRecord:
    """Parse one bounded, no-follow transaction record."""
    return parse_transaction_record(_read_json(path, trusted_root=trusted_root))


def read_owned_record(path: Path, workspace_root: Path, generation: int) -> TransactionRecord:
    """Read a record only when its target paths remain inside this workspace."""
    from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions

    record = read_record(path, trusted_root=workspace_root)
    transitions.assert_record_ownership(workspace_root, record, generation)
    return record


def _write_record(
    path: Path, record: TransactionRecord, *, trusted_root: Path
) -> None:
    validate_transaction_record(record)
    payload: dict[str, JsonValue] = {
        "schema_version": 2,
        "generation": record.generation,
        "cas_token": record.cas_token,
        "phase": record.phase,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "catalog_sha256": record.catalog_sha256,
        "targets": [asdict(target) for target in record.targets],
        "processes": [asdict(process) for process in record.processes],
        "error": record.error,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    atomic_bytes(path, encoded, 0o600, trusted_root=trusted_root)


def _publish_record(
    path: Path,
    current: TransactionRecord,
    expected: TransactionRecord,
    record: TransactionRecord,
    *,
    trusted_root: Path,
) -> TransactionRecord:
    from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions

    if current != expected:
        raise TransactionBlockedError("workspace transaction predecessor changed")
    transitions.assert_transition(current, record)
    successor = replace(record, cas_token=transitions.token(), updated_at=transitions.timestamp())
    transitions.assert_transition(current, successor)
    _write_record(path, successor, trusted_root=trusted_root)
    return successor


def _read_json(
    path: Path, *, trusted_root: Path | None = None
) -> dict[str, JsonValue]:
    root = trusted_root if trusted_root is not None else path.parent
    raw = _read_authorized_regular(
        path, _MAX_DOCUMENT_BYTES, trusted_root=root, private=True
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace transaction JSON is invalid") from error
    return dict(require_mapping(value, "workspace transaction"))
