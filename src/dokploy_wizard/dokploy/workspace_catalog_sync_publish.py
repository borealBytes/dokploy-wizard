"""Atomic publication through descriptor-authorized workspace parents."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    TargetReceipt,
    TransactionBlockedError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import (
    PathAuthority,
    authorized_parent,
    snapshot_at,
)


def atomic_bytes(
    path: Path,
    content: bytes,
    mode: int,
    *,
    trusted_root: Path,
    expected: TargetReceipt | None = None,
) -> None:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        _require_expected(authority, path, expected)
        temporary = f".{authority.name}.{secrets.token_hex(16)}.tmp"
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                mode,
                dir_fd=authority.descriptor,
            )
            try:
                _write_all(descriptor, content)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            authority.verify()
            _require_expected(authority, path, expected)
            os.replace(
                temporary,
                authority.name,
                src_dir_fd=authority.descriptor,
                dst_dir_fd=authority.descriptor,
            )
            published = True
            os.fsync(authority.descriptor)
        finally:
            if not published:
                _unlink_at(temporary, authority.descriptor)


def create_bytes(path: Path, content: bytes, mode: int, *, trusted_root: Path) -> None:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        temporary = f".{authority.name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                mode,
                dir_fd=authority.descriptor,
            )
            try:
                _write_all(descriptor, content)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            authority.verify()
            try:
                os.link(
                    temporary,
                    authority.name,
                    src_dir_fd=authority.descriptor,
                    dst_dir_fd=authority.descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise TransactionBlockedError(
                    "workspace legacy adoption receipt already exists"
                ) from error
            os.fsync(authority.descriptor)
        finally:
            _unlink_at(temporary, authority.descriptor)
            os.fsync(authority.descriptor)


def atomic_symlink(
    path: Path,
    target: str,
    *,
    trusted_root: Path,
    expected: TargetReceipt | None = None,
) -> None:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        _require_expected(authority, path, expected)
        temporary = f".{authority.name}.{secrets.token_hex(16)}.tmp"
        published = False
        try:
            os.symlink(target, temporary, dir_fd=authority.descriptor)
            authority.verify()
            _require_expected(authority, path, expected)
            os.replace(
                temporary,
                authority.name,
                src_dir_fd=authority.descriptor,
                dst_dir_fd=authority.descriptor,
            )
            published = True
            os.fsync(authority.descriptor)
        finally:
            if not published:
                _unlink_at(temporary, authority.descriptor)


def unlink_expected(path: Path, expected: TargetReceipt, *, trusted_root: Path) -> None:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        _require_expected(authority, path, expected)
        os.unlink(authority.name, dir_fd=authority.descriptor)
        os.fsync(authority.descriptor)


def _require_expected(
    authority: PathAuthority,
    path: Path,
    expected: TargetReceipt | None,
) -> None:
    if expected is None:
        return
    current = snapshot_at(authority, path)
    expected_identity = (
        expected.pre_state,
        expected.pre_sha256,
        expected.pre_mode,
        expected.pre_target,
    )
    current_identity = (
        current.pre_state,
        current.pre_sha256,
        current.pre_mode,
        current.pre_target,
    )
    if current_identity != expected_identity:
        raise TransactionBlockedError("workspace target changed at publication boundary")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]


def _unlink_at(name: str, descriptor: int) -> None:
    try:
        os.unlink(name, dir_fd=descriptor)
    except FileNotFoundError:
        return
