from __future__ import annotations

import fcntl
import os
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Final

from dokploy_wizard.dokploy.coder_migration_receipt_semantics import validate_receipt_transition
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    ReceiptSchemaError,
    ReceiptUpdate,
    new_migration_receipt,
    new_migration_step,
    receipt_bytes,
)
from dokploy_wizard.dokploy.coder_migration_receipt_validation import parse_receipt

_FILE_MODE: Final = 0o600
_DIRECTORY_MODE: Final = 0o700
_MAX_RECEIPT_BYTES: Final = 1024 * 1024
_FILENAME: Final = "coder-migration-receipts-v1.json"
_LOCK_FILENAME: Final = ".coder-migration-receipts-v1.lock"


class ReceiptConcurrencyError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


class CoderMigrationReceiptStore:
    def __init__(
        self, state_dir: Path, *, token_factory: Callable[[], str] = secrets.token_urlsafe
    ) -> None:
        self._state_dir = state_dir
        self._token_factory = token_factory

    def create(self, receipt: MigrationReceipt) -> MigrationReceipt:
        self._secure_directory()
        descriptor = self._acquire_lock()
        try:
            if self.load() is not None:
                raise ReceiptConcurrencyError("migration receipt already exists")
            stored = replace(receipt, cas_token=self._token())
            payload = receipt_bytes(stored)
            parse_receipt(payload)
            self._atomic_write(payload)
            return stored
        finally:
            self._release_lock(descriptor)

    def load(self) -> MigrationReceipt | None:
        self._secure_directory()
        payload = self._read()
        return None if payload is None else parse_receipt(payload)

    def checkpoint(self, expected: MigrationReceipt, update: ReceiptUpdate) -> MigrationReceipt:
        descriptor = self._acquire_lock()
        try:
            current = self.load()
            if current is None or current != expected:
                raise ReceiptConcurrencyError("migration receipt generation or token changed")
            next_receipt = replace(
                expected,
                generation=expected.generation + 1,
                cas_token=self._token(),
                status=update.status,
                updated_at=update.updated_at,
                steps=update.steps,
                post_inventory_sha256=update.post_inventory_sha256,
            )
            validate_receipt_transition(current, next_receipt)
            payload = receipt_bytes(next_receipt)
            parse_receipt(payload)
            self._atomic_write(payload)
            return next_receipt
        finally:
            self._release_lock(descriptor)

    def _secure_directory(self) -> None:
        try:
            self._state_dir.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            metadata = self._state_dir.lstat()
        except OSError as error:
            raise ReceiptSchemaError("migration receipt directory is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ReceiptSchemaError("migration receipt directory is invalid")
        os.chmod(self._state_dir, _DIRECTORY_MODE)

    def _read(self) -> bytes | None:
        path = self._state_dir / _FILENAME
        directory_identity = _identity(self._state_dir.lstat())
        try:
            path_metadata = path.lstat()
        except FileNotFoundError:
            return None
        initial = _receipt_identity(path_metadata)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise ReceiptSchemaError("migration receipt changed while opening") from error
        try:
            if _identity(os.fstat(descriptor)) != initial:
                raise ReceiptSchemaError("migration receipt identity changed")
            payload = _read_exact(descriptor, initial.size)
            if _identity(os.fstat(descriptor)) != initial:
                raise ReceiptSchemaError("migration receipt changed while reading")
        finally:
            os.close(descriptor)
        try:
            final = _receipt_identity(path.lstat())
        except FileNotFoundError as error:
            raise ReceiptSchemaError("migration receipt changed while reading") from error
        if final != initial or _identity(self._state_dir.lstat()) != directory_identity:
            raise ReceiptSchemaError("migration receipt changed while reading")
        return payload

    def _atomic_write(self, payload: bytes) -> None:
        path = self._state_dir / _FILENAME
        temporary = self._state_dir / f".{_FILENAME}.{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        temporary_identity: _FileIdentity | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            temporary_identity = _identity(os.fstat(descriptor))
            _write_all(descriptor, payload)
            os.fchmod(descriptor, _FILE_MODE)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            _fsync_directory(self._state_dir)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            _remove_owned_temp(temporary, temporary_identity)
            raise ReceiptSchemaError("migration receipt write failed") from error

    def _acquire_lock(self) -> int:
        path = self._state_dir / _LOCK_FILENAME
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        os.fchmod(descriptor, _FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _release_lock(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _token(self) -> str:
        token = self._token_factory()
        if not token:
            raise ReceiptSchemaError("receipt token factory returned an empty token")
        return token


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    payload = bytearray()
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        payload.extend(chunk)
        remaining -= len(chunk)
    if len(payload) != expected_size:
        raise ReceiptSchemaError("migration receipt has a short or oversized read")
    return bytes(payload)


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _receipt_identity(metadata: os.stat_result) -> _FileIdentity:
    identity = _identity(metadata)
    if (
        stat.S_ISLNK(identity.mode)
        or not stat.S_ISREG(identity.mode)
        or stat.S_IMODE(identity.mode) != _FILE_MODE
        or identity.size > _MAX_RECEIPT_BYTES
    ):
        raise ReceiptSchemaError("migration receipt metadata is invalid")
    return identity


def _remove_owned_temp(path: Path, expected: _FileIdentity | None) -> None:
    if expected is None:
        return
    try:
        if _identity(path.lstat()) == expected:
            path.unlink()
    except FileNotFoundError:
        return


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CoderMigrationReceiptStore",
    "ReceiptConcurrencyError",
    "ReceiptSchemaError",
    "ReceiptUpdate",
    "new_migration_receipt",
    "new_migration_step",
]
