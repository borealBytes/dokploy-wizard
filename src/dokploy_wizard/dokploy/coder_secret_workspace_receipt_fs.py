from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Final

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError

_FILE_MODE: Final = 0o600
_DIRECTORY_MODE: Final = 0o700
_MAX_RECEIPT_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    owner: int
    size: int
    mtime_ns: int
    ctime_ns: int


def read_receipt_bytes(state_dir: Path, filename: str) -> bytes | None:
    directory = _secure_directory(state_dir)
    path = state_dir / filename
    try:
        initial = _receipt_identity(path.lstat())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _read_error() from error
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise _read_error() from error
    try:
        if _receipt_identity(os.fstat(descriptor)) != initial:
            raise _read_error()
        payload = _read_exact(descriptor, initial.size)
        if _receipt_identity(os.fstat(descriptor)) != initial:
            raise _read_error()
    except OSError as error:
        raise _read_error() from error
    finally:
        os.close(descriptor)
    try:
        if (
            _receipt_identity(path.lstat()) != initial
            or _directory_identity(state_dir.lstat()) != directory
        ):
            raise _read_error()
    except OSError as error:
        raise _read_error() from error
    return payload


def write_receipt_bytes(state_dir: Path, filename: str, payload: bytes) -> None:
    _secure_directory(state_dir)
    path = state_dir / filename
    temporary = state_dir / f".{filename}.{token_hex(12)}.tmp"
    descriptor: int | None = None
    identity: _FileIdentity | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        identity = _receipt_identity(os.fstat(descriptor))
        _write_all(descriptor, payload)
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(state_dir)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        _remove_owned_temp(temporary, identity)
        raise CoderSecretClientError(
            "Coder workspace verification receipt cannot be written"
        ) from error


def _secure_directory(path: Path) -> _FileIdentity:
    try:
        path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        return _directory_identity(path.lstat())
    except OSError as error:
        raise _read_error() from error


def _directory_identity(metadata: os.stat_result) -> _FileIdentity:
    identity = _identity(metadata)
    if (
        not stat.S_ISDIR(identity.mode)
        or stat.S_IMODE(identity.mode) != _DIRECTORY_MODE
        or identity.owner != os.geteuid()
    ):
        raise _read_error()
    return identity


def _receipt_identity(metadata: os.stat_result) -> _FileIdentity:
    identity = _identity(metadata)
    if (
        not stat.S_ISREG(identity.mode)
        or stat.S_IMODE(identity.mode) != _FILE_MODE
        or identity.owner != os.geteuid()
        or identity.size > _MAX_RECEIPT_BYTES
    ):
        raise _read_error()
    return identity


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        owner=metadata.st_uid,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    payload = bytearray()
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        payload.extend(chunk)
        remaining -= len(chunk)
    if len(payload) != expected_size:
        raise _read_error()
    return bytes(payload)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]


def _remove_owned_temp(path: Path, expected: _FileIdentity | None) -> None:
    if expected is None:
        return
    try:
        if _identity(path.lstat()) == expected:
            path.unlink()
    except OSError:
        return


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_error() -> CoderSecretClientError:
    return CoderSecretClientError("Coder workspace verification receipt cannot be read")
