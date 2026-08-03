"""Canonical mode-0600 atomic JSON I/O for the Shared Core state upgrade."""

from __future__ import annotations

import json
import os
import secrets
import stat
from hashlib import sha256
from pathlib import Path
from typing import Literal, Mapping, overload

from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_intent import StateUpgradeError

_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


def canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    atomic_bytes(path, canonical_bytes(payload))


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError):
        _unlink_without_masking(temporary)
        raise


@overload
def read_json(path: Path) -> dict[str, object]: ...


@overload
def read_json(path: Path, *, json_values: Literal[True]) -> dict[str, JsonValue]: ...


def read_json(
    path: Path, *, json_values: bool = False
) -> dict[str, object] | dict[str, JsonValue]:
    del json_values
    data = _read_regular(path)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as error:
        raise StateUpgradeError(f"State upgrade file {path.name} is malformed.") from error
    if not isinstance(payload, dict):
        raise StateUpgradeError(f"State upgrade file {path.name} must be an object.")
    return payload


def file_hash(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return sha256(_read_regular(path)).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise StateUpgradeError(f"State upgrade file {path.name} is missing.") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise StateUpgradeError(f"State upgrade file {path.name} must be regular.")
    if metadata.st_size > _MAX_DOCUMENT_BYTES:
        raise StateUpgradeError(f"State upgrade file {path.name} is oversized.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise StateUpgradeError(f"State upgrade file {path.name} changed before reading.")
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise StateUpgradeError(f"State upgrade file {path.name} changed while reading.")
    if identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
        raise StateUpgradeError(f"State upgrade file {path.name} changed while reading.")
    data = b"".join(chunks)
    if len(data) != metadata.st_size:
        raise StateUpgradeError(f"State upgrade file {path.name} was not read exactly.")
    return data


def _unlink_without_masking(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
