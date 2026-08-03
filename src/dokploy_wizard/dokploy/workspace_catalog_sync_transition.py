"""Pure durable transition checks for workspace catalog transactions."""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256 as _sha256
from pathlib import Path

from dokploy_wizard.dokploy import workspace_catalog_sync_invariants as _invariants
from dokploy_wizard.dokploy.workspace_catalog_sync_invariants import (
    PROCESS_EDGES,
    TARGET_EDGES,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogTarget,
    ProcessIdentity,
    ProcessStatus,
    TargetStatus,
    TransactionPhase,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)


def target_status(
    record: TransactionRecord, index: int, status: TargetStatus, *, post: bool = False
) -> TransactionRecord:
    targets = list(record.targets)
    target = targets[index]
    if status not in TARGET_EDGES[target.status]:
        raise WorkspaceCatalogSyncError("workspace target transition is invalid")
    targets[index] = replace(
        target,
        status=status,
        post_sha256=target.staged_sha256 if post else target.post_sha256,
    )
    return replace(record, targets=tuple(targets))


def process_status(
    record: TransactionRecord, index: int, status: ProcessStatus
) -> TransactionRecord:
    processes = list(record.processes)
    previous = processes[index]
    if status not in PROCESS_EDGES[previous.status]:
        raise WorkspaceCatalogSyncError("workspace process transition is invalid")
    processes[index] = replace(previous, status=status)
    return replace(record, processes=tuple(processes))


def replace_process(
    record: TransactionRecord, index: int, process: ProcessIdentity
) -> TransactionRecord:
    """Persist the independently observed replacement process identity."""
    processes = list(record.processes)
    processes[index] = process
    return replace(record, processes=tuple(processes))


def validate_processes(
    processes: tuple[ProcessIdentity, ...], generation: int
) -> tuple[ProcessIdentity, ...]:
    """Validate process ownership before transaction preparation."""
    if any(process.generation != generation for process in processes):
        raise WorkspaceCatalogSyncError("workspace process generation does not match transaction")
    if any(process.status != "observed" for process in processes):
        raise WorkspaceCatalogSyncError(
            "workspace process must be observed before preparation"
        )
    if len({process.name for process in processes}) != len(processes):
        raise WorkspaceCatalogSyncError("workspace process names are not unique")
    return processes


def assert_workspace_path(workspace_root: Path, target: Path) -> None:
    """Reject target paths whose existing parent symlinks leave the workspace."""
    root = workspace_root.resolve(strict=True)
    parent = target.absolute().parent.resolve(strict=False)
    try:
        parent.relative_to(root)
    except ValueError as error:
        raise WorkspaceCatalogSyncError("workspace transaction target escapes workspace") from error


def assert_record_ownership(
    workspace_root: Path, record: TransactionRecord, generation: int
) -> None:
    """Require a read record belongs to this generation and workspace tree."""
    if record.generation != generation:
        raise WorkspaceCatalogSyncError("workspace transaction generation does not match")
    for receipt in record.targets:
        assert_workspace_path(workspace_root, Path(receipt.path))


def assert_transition(previous: TransactionRecord, next_record: TransactionRecord) -> None:
    _invariants.assert_transition(previous, next_record)


def validate_record_invariants(record: TransactionRecord) -> None:
    _invariants.validate_record_invariants(record)


def inferred_blocked_phase(record: TransactionRecord) -> TransactionPhase:
    return _invariants.inferred_blocked_phase(record)


def catalog_sha(targets: tuple[CatalogTarget, ...]) -> str:
    """Hash the catalog target projection in canonical order."""
    payload = [
        {
            "kind": target.kind,
            "path": str(target.path),
            "sha256": _sha256(target.content).hexdigest(),
        }
        for target in targets
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded).hexdigest()


def token() -> str:
    """Return one 256-bit CAS token."""
    return secrets.token_hex(32)


def timestamp() -> str:
    """Return a canonical UTC second timestamp."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def crash_at(requested: str | None, phase: str) -> None:
    """Inject a deterministic crash after the named durable phase."""
    if requested == phase:
        raise RuntimeError("injected crash")
