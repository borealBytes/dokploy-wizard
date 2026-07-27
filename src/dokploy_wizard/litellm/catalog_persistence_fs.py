from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MAX_PERSISTED_BYTES = 16 * 1024 * 1024


class CatalogPersistenceError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    owned_identity: tuple[int, int] | None = None
    try:
        _require_absent_temporary(temporary)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        opened = os.fstat(descriptor)
        owned_identity = (opened.st_dev, opened.st_ino)
        _write_all(descriptor, payload)
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        owned_identity = None
        _fsync_directory(path.parent)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        _remove_owned_temporary(temporary, owned_identity)
        raise CatalogPersistenceError(
            f"atomic state write failed: {error.errno}"
        ) from error


def publish_immutable(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    owned_identity: tuple[int, int] | None = None
    try:
        _require_absent_temporary(temporary)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        opened = os.fstat(descriptor)
        owned_identity = (opened.st_dev, opened.st_ino)
        _write_all(descriptor, payload)
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        owned_identity = None
        _fsync_directory(path.parent)
    except FileExistsError as error:
        if descriptor >= 0:
            os.close(descriptor)
        _remove_owned_temporary(temporary, owned_identity)
        if read_contract_file(path, "generation") == payload:
            return
        raise CatalogPersistenceError("immutable generation already exists") from error
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        _remove_owned_temporary(temporary, owned_identity)
        raise CatalogPersistenceError(
            f"immutable generation write failed: {error.errno}"
        ) from error


def validate_existing_contract(state_root: Path, state_filename: str) -> bytes | None:
    if not state_root.exists() and not state_root.is_symlink():
        return None
    _require_directory(state_root)
    generations = state_root / "generations"
    if generations.exists() or generations.is_symlink():
        _require_directory(generations)
    return read_contract_file(state_root / state_filename, "state")


def secure_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        try:
            path.mkdir(mode=_DIRECTORY_MODE)
        except OSError as error:
            raise CatalogPersistenceError(
                f"state directory creation failed: {error.errno}"
            ) from error
    _require_directory(path)


def read_contract_file(path: Path, label: str) -> bytes | None:
    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_metadata.st_mode):
        raise CatalogPersistenceError(f"{label} file is a symlink")
    if not stat.S_ISREG(path_metadata.st_mode):
        raise CatalogPersistenceError(f"{label} path is not a regular file")
    if stat.S_IMODE(path_metadata.st_mode) != _FILE_MODE:
        raise CatalogPersistenceError(f"{label} file mode is invalid")
    if path_metadata.st_size > _MAX_PERSISTED_BYTES:
        raise CatalogPersistenceError(f"{label} file is too large")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise CatalogPersistenceError(
            f"{label} file open failed: {error.errno}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (path_metadata.st_dev, path_metadata.st_ino):
            raise CatalogPersistenceError(f"{label} file identity changed")
        payload = _read_exact(descriptor, path_metadata.st_size)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    identity = (path_metadata.st_dev, path_metadata.st_ino, path_metadata.st_size)
    if (final.st_dev, final.st_ino, final.st_size) != identity:
        raise CatalogPersistenceError(f"{label} file metadata changed")
    current_identity = (current.st_dev, current.st_ino, current.st_size)
    if current_identity != identity or len(payload) != path_metadata.st_size:
        raise CatalogPersistenceError(f"{label} file path changed")
    return payload


def _require_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise CatalogPersistenceError(f"{path.name} directory is a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise CatalogPersistenceError(f"{path.name} path is not a directory")
    if stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE:
        raise CatalogPersistenceError(f"{path.name} directory mode is invalid")


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]


def _require_absent_temporary(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise CatalogPersistenceError("temporary path already exists")


def _remove_owned_temporary(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
