"""Descriptor-rooted filesystem operations for workspace catalog transactions."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    TargetReceipt,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_IDENTITY: TypeAlias = tuple[int, int, int, int, int, int]
_DIRECTORY_IDENTITY: TypeAlias = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class PathAuthority:
    root: Path
    components: tuple[str, ...]
    identities: tuple[_DIRECTORY_IDENTITY, ...]
    descriptor: int
    name: str

    def verify(self) -> None:
        try:
            descriptors = _open_directories(self.root, self.components)
        except OSError as error:
            raise TransactionBlockedError(
                "workspace path parent changed during transaction"
            ) from error
        try:
            identities = tuple(_directory_identity(os.fstat(item)) for item in descriptors)
        finally:
            _close_all(descriptors)
        if identities != self.identities:
            raise TransactionBlockedError("workspace path parent changed during transaction")


@contextmanager
def authorized_parent(path: Path, *, trusted_root: Path) -> Iterator[PathAuthority]:
    root, parts = _relative_parts(path, trusted_root)
    if not parts:
        raise WorkspaceCatalogSyncError("workspace path has no authorized leaf")
    try:
        descriptors = _open_directories(root, parts[:-1])
    except OSError as error:
        raise TransactionBlockedError("workspace path parent is unsafe") from error
    authority = PathAuthority(
        root=root,
        components=parts[:-1],
        identities=tuple(_directory_identity(os.fstat(item)) for item in descriptors),
        descriptor=descriptors[-1],
        name=parts[-1],
    )
    try:
        authority.verify()
        yield authority
        authority.verify()
    finally:
        _close_all(descriptors)


def ensure_authorized_directory(
    path: Path, *, trusted_root: Path, private: bool
) -> None:
    root, parts = _relative_parts(path, trusted_root)
    descriptors = _open_directories(root, ())
    try:
        for component in parts:
            parent = descriptors[-1]
            try:
                descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=parent)
                os.fsync(parent)
                descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            descriptors.append(descriptor)
            if private:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
                os.fsync(parent)
        expected = tuple(_directory_identity(os.fstat(item)) for item in descriptors)
        verification = _open_directories(root, parts)
        try:
            actual = tuple(_directory_identity(os.fstat(item)) for item in verification)
        finally:
            _close_all(verification)
        if actual != expected:
            raise TransactionBlockedError("workspace directory changed during authorization")
    except OSError as error:
        raise TransactionBlockedError("workspace directory is unsafe") from error
    finally:
        _close_all(descriptors)


def read_regular(
    path: Path,
    limit: int,
    *,
    trusted_root: Path,
    private: bool,
) -> bytes:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        return _read_regular_at(authority, limit=limit, private=private)


def target_snapshot(path: Path, *, trusted_root: Path) -> TargetReceipt:
    with authorized_parent(path, trusted_root=trusted_root) as authority:
        return snapshot_at(authority, path)


def snapshot_at(authority: PathAuthority, path: Path) -> TargetReceipt:
    try:
        metadata = os.stat(
            authority.name, dir_fd=authority.descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return _absent_receipt(path)
    if stat.S_ISREG(metadata.st_mode):
        content = _read_regular_at(authority, limit=metadata.st_size, private=False)
        return TargetReceipt(
            path=str(path), kind="file", pre_state="file", pre_sha256=_sha256(content),
            pre_mode=f"{stat.S_IMODE(metadata.st_mode):04o}", pre_target=None,
            post_mode=None, staged_sha256="", post_sha256=None, status="observed",
        )
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(authority.name, dir_fd=authority.descriptor)
        after = os.stat(
            authority.name, dir_fd=authority.descriptor, follow_symlinks=False
        )
        authority.verify()
        if _file_identity(metadata) != _file_identity(after):
            raise TransactionBlockedError("workspace symlink changed while reading")
        return TargetReceipt(
            path=str(path), kind="symlink", pre_state="symlink",
            pre_sha256=_sha256(target.encode()), pre_mode=None, pre_target=target,
            post_mode=None, staged_sha256="", post_sha256=None, status="observed",
        )
    raise TransactionBlockedError(
        "workspace target must be absent, a regular file, or a symlink"
    )


def _read_regular_at(
    authority: PathAuthority, *, limit: int, private: bool
) -> bytes:
    try:
        descriptor = os.open(
            authority.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=authority.descriptor,
        )
    except OSError as error:
        raise TransactionBlockedError("workspace transaction file is inaccessible") from error
    try:
        return _read_descriptor(
            descriptor,
            authority=authority,
            limit=limit,
            private=private,
        )
    finally:
        os.close(descriptor)


def _read_descriptor(
    descriptor: int,
    *,
    authority: PathAuthority,
    limit: int,
    private: bool,
) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise TransactionBlockedError("workspace transaction file is invalid")
    if private and stat.S_IMODE(before.st_mode) != 0o600:
        raise TransactionBlockedError("workspace transaction sensitive file is invalid")
    chunks: list[bytes] = []
    remaining = before.st_size + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    pathname = os.stat(
        authority.name, dir_fd=authority.descriptor, follow_symlinks=False
    )
    authority.verify()
    content = b"".join(chunks)
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(pathname)
        or len(content) != before.st_size
    ):
        raise TransactionBlockedError("workspace transaction file changed while reading")
    return content


def _relative_parts(path: Path, trusted_root: Path) -> tuple[Path, tuple[str, ...]]:
    root = trusted_root.resolve(strict=True)
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as error:
        raise WorkspaceCatalogSyncError("workspace path escapes trusted root") from error
    if ".." in relative.parts:
        raise WorkspaceCatalogSyncError("workspace path escapes trusted root")
    return root, relative.parts


def _open_directories(root: Path, components: tuple[str, ...]) -> list[int]:
    descriptors = [os.open(root, _DIRECTORY_FLAGS)]
    try:
        for component in components:
            descriptors.append(os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptors[-1]))
    except OSError:
        _close_all(descriptors)
        raise
    return descriptors


def _absent_receipt(path: Path) -> TargetReceipt:
    return TargetReceipt(
        path=str(path), kind="file", pre_state="absent", pre_sha256=None,
        pre_mode=None, pre_target=None, post_mode=None, staged_sha256="",
        post_sha256=None, status="observed",
    )


def _directory_identity(metadata: os.stat_result) -> _DIRECTORY_IDENTITY:
    if not stat.S_ISDIR(metadata.st_mode):
        raise TransactionBlockedError("workspace path component is not a directory")
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _file_identity(metadata: os.stat_result) -> _FILE_IDENTITY:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _close_all(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
