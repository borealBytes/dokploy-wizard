"""Target staging, publication checks, and preimage restoration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dokploy_wizard.dokploy.workspace_catalog_sync_io import (
    atomic_bytes,
    atomic_symlink,
    sha256,
    target_snapshot,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogTarget,
    TargetReceipt,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import read_regular
from dokploy_wizard.dokploy.workspace_catalog_sync_pointers import render_owned_target
from dokploy_wizard.dokploy.workspace_catalog_sync_publish import unlink_expected


def stage_target(
    target: CatalogTarget,
    *,
    preimage_path: Path,
    staged_path: Path,
    explicit_operator_update: bool,
    trusted_root: Path,
) -> TargetReceipt:
    snapshot = target_snapshot(target.path, trusted_root=trusted_root)
    if snapshot.pre_state != "absent" and not explicit_operator_update:
        raise TransactionBlockedError("workspace automatic transition is forbidden")
    current: bytes | None = None
    if snapshot.pre_state == "file":
        if snapshot.pre_sha256 is None:
            raise WorkspaceCatalogSyncError("workspace file preimage is incomplete")
        current = _read_bytes(
            target.path,
            snapshot.pre_sha256,
            private=False,
            trusted_root=trusted_root,
        )
        atomic_bytes(preimage_path, current, 0o600, trusted_root=trusted_root)
    elif snapshot.pre_state == "symlink":
        if snapshot.pre_target is None:
            raise WorkspaceCatalogSyncError("workspace symlink preimage is incomplete")
        current = snapshot.pre_target.encode()
    staged_content = render_owned_target(target, current)
    atomic_bytes(staged_path, staged_content, 0o600, trusted_root=trusted_root)
    return replace(
        snapshot,
        kind=target.kind,
        post_mode=(
            snapshot.pre_mode
            if target.kind == "file" and snapshot.pre_state == "file"
            else f"{target.mode:04o}" if target.kind == "file" else None
        ),
        staged_sha256=sha256(staged_content),
        status="prepared",
    )


def write_staged_target(
    receipt: TargetReceipt, *, staged_path: Path, trusted_root: Path
) -> None:
    content = _read_bytes(
        staged_path,
        receipt.staged_sha256,
        private=True,
        trusted_root=trusted_root,
    )
    if receipt.kind == "file":
        if receipt.post_mode is None:
            raise WorkspaceCatalogSyncError("workspace target post mode is incomplete")
        atomic_bytes(
            Path(receipt.path),
            content,
            int(receipt.post_mode, 8),
            trusted_root=trusted_root,
            expected=receipt,
        )
        return
    atomic_symlink(
        Path(receipt.path),
        content.decode("utf-8"),
        trusted_root=trusted_root,
        expected=receipt,
    )


def verify_staged_target(
    receipt: TargetReceipt, *, staged_path: Path, trusted_root: Path
) -> TargetReceipt:
    current = target_snapshot(Path(receipt.path), trusted_root=trusted_root)
    if current.pre_state != receipt.kind or current.pre_sha256 != receipt.staged_sha256:
        raise TransactionBlockedError("workspace target changed before transaction switch")
    if (
        receipt.kind == "file"
        and current.pre_mode != receipt.post_mode
    ):
        raise TransactionBlockedError("workspace target mode changed before transaction switch")
    _read_bytes(
        staged_path,
        receipt.staged_sha256,
        private=True,
        trusted_root=trusted_root,
    )
    return current


def matches_preimage(receipt: TargetReceipt, *, trusted_root: Path) -> bool:
    current = target_snapshot(Path(receipt.path), trusted_root=trusted_root)
    return (current.pre_state, current.pre_sha256, current.pre_mode, current.pre_target) == (
        receipt.pre_state,
        receipt.pre_sha256,
        receipt.pre_mode,
        receipt.pre_target,
    )


def restore_preimage(
    receipt: TargetReceipt,
    *,
    preimage_path: Path,
    trusted_root: Path,
    expected: TargetReceipt,
) -> None:
    path = Path(receipt.path)
    if receipt.pre_state == "absent":
        unlink_expected(path, expected, trusted_root=trusted_root)
        return
    if receipt.pre_state == "symlink":
        if receipt.pre_target is None:
            raise WorkspaceCatalogSyncError("workspace symlink preimage is incomplete")
        atomic_symlink(
            path,
            receipt.pre_target,
            trusted_root=trusted_root,
            expected=expected,
        )
        return
    content = _read_bytes(
        preimage_path,
        receipt.pre_sha256 or "",
        private=True,
        trusted_root=trusted_root,
    )
    if (
        receipt.pre_sha256 is None
        or sha256(content) != receipt.pre_sha256
        or receipt.pre_mode is None
    ):
        raise WorkspaceCatalogSyncError("workspace file preimage is incomplete")
    atomic_bytes(
        path,
        content,
        int(receipt.pre_mode, 8),
        trusted_root=trusted_root,
        expected=expected,
    )


def _read_bytes(
    path: Path,
    expected_sha256: str,
    *,
    private: bool,
    trusted_root: Path,
) -> bytes:
    try:
        content = read_regular(
            path,
            2 * 1024 * 1024,
            trusted_root=trusted_root,
            private=private,
        )
    except TransactionBlockedError as error:
        raise TransactionBlockedError(
            "workspace staged content does not match transaction"
        ) from error
    if sha256(content) != expected_sha256:
        raise TransactionBlockedError("workspace staged content does not match transaction")
    return content
