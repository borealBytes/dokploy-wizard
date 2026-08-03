"""Durable canonical documents for Shared Core sync mutations and leases."""

from __future__ import annotations

import json
import os
import secrets
import stat
from hashlib import sha256
from pathlib import Path
from typing import Final

from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    DisableTombstone,
    ScheduleMutationReceipt,
)
from dokploy_wizard.dokploy.shared_core_schedule_teardown import ScheduleTeardownReceipt
from dokploy_wizard.state.sync_schema import JsonValue, SyncStateError, canonical_digest

_MAX_DOCUMENT_BYTES: Final = 256 * 1024


class SyncArtifactStore:
    """Persist and verify immutable schedule receipts and disable tombstones."""

    def __init__(self, state_dir: Path) -> None:
        self._root = state_dir / "shared-core-sync"

    def persist_schedule_receipt(self, receipt: ScheduleMutationReceipt) -> str:
        digest = receipt.sha256()
        self._persist("schedule-receipts", digest, receipt.to_dict())
        return digest

    def load_schedule_receipt(self, digest: str) -> ScheduleMutationReceipt:
        return ScheduleMutationReceipt.from_dict(self._load("schedule-receipts", digest))

    def persist_disable_tombstone(self, tombstone: DisableTombstone) -> str:
        digest = tombstone.sha256()
        self._persist("disable-tombstones", digest, tombstone.to_dict())
        return digest

    def load_disable_tombstone(self, digest: str) -> DisableTombstone:
        return DisableTombstone.from_dict(self._load("disable-tombstones", digest))

    def persist_schedule_teardown(self, receipt: ScheduleTeardownReceipt) -> str:
        digest = receipt.sha256()
        self._persist("schedule-teardowns", digest, receipt.to_dict())
        pointer = self._teardown_pointer(receipt.schedule_id)
        self._persist_named(pointer, receipt.to_dict())
        return digest

    def load_schedule_teardown(self, schedule_id: str) -> ScheduleTeardownReceipt | None:
        pointer = self._teardown_pointer(schedule_id)
        if not pointer.exists():
            return None
        return ScheduleTeardownReceipt.from_dict(_decode_stable(pointer))

    def _teardown_pointer(self, schedule_id: str) -> Path:
        name = sha256(schedule_id.encode()).hexdigest()
        return self._root / "schedule-teardown-index" / f"{name}.json"

    def _persist(self, directory: str, digest: str, payload: dict[str, JsonValue]) -> None:
        encoded = _canonical_bytes(payload)
        if canonical_digest(payload) != digest:
            raise SyncStateError("Sync artifact digest does not match canonical bytes.")
        target_dir = self._root / directory
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target_dir, 0o700)
        target = target_dir / f"{digest}.json"
        if target.exists():
            if _read_stable(target) != encoded:
                raise SyncStateError("Existing sync artifact bytes do not match their digest.")
            return
        temporary = target_dir / f".{target.name}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, target)
            _fsync_directory(target_dir)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _load(self, directory: str, digest: str) -> dict[str, JsonValue]:
        path = self._root / directory / f"{digest}.json"
        raw = _read_stable(path)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SyncStateError("Sync artifact JSON is malformed.") from error
        if not isinstance(payload, dict) or canonical_digest(payload) != digest:
            raise SyncStateError("Sync artifact does not match its requested digest.")
        return payload

    def _persist_named(self, target: Path, payload: dict[str, JsonValue]) -> None:
        encoded = _canonical_bytes(payload)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if target.exists():
            if _read_stable(target) != encoded:
                raise SyncStateError("Existing named sync artifact does not match.")
            return
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        _fsync_directory(target.parent)


def _canonical_bytes(payload: dict[str, JsonValue]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _decode_stable(path: Path) -> dict[str, JsonValue]:
    try:
        payload = json.loads(_read_stable(path))
    except json.JSONDecodeError as error:
        raise SyncStateError("Sync artifact JSON is malformed.") from error
    if not isinstance(payload, dict):
        raise SyncStateError("Sync artifact must be an object.")
    return payload


def _read_stable(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise SyncStateError("Sync artifact must be a mode-0600 regular file.")
        if before.st_size > _MAX_DOCUMENT_BYTES:
            raise SyncStateError("Sync artifact exceeds its byte limit.")
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
    current = path.stat(follow_symlinks=False)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SyncStateError("Sync artifact changed while it was read.")
    if identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
        raise SyncStateError("Sync artifact pathname identity changed while it was read.")
    raw = b"".join(chunks)
    if len(raw) != before.st_size:
        raise SyncStateError("Sync artifact was not read exactly.")
    return raw


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
