"""Strict durable receipt storage for OpenCode Go cutovers."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dokploy_wizard.litellm.opencode_go_cutover_receipt_schema import (
    canonical_receipt_bytes as canonical_receipt_bytes,
)
from dokploy_wizard.litellm.opencode_go_cutover_receipt_schema import (
    parse_receipt as parse_receipt,
)
from dokploy_wizard.litellm.opencode_go_cutover_receipt_schema import (
    receipt_to_dict as receipt_to_dict,
)
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverReceipt,
    OpenCodeGoCutoverBlockedError,
    OpenCodeGoCutoverError,
)

_RECEIPT_NAME = "litellm-cutover-receipt-v1.json"
_MAX_RECEIPT_BYTES = 256 * 1024
class CutoverReceiptStore:
    """Atomically persist the single receipt kept in the LiteLLM metadata volume."""

    def __init__(self, state_root: Path) -> None:
        self._root = state_root / "opencode-go"
        self._path = self._root / _RECEIPT_NAME
        self._lock_path = self._root / ".litellm-cutover.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        import fcntl

        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OpenCodeGoCutoverBlockedError("OpenCode Go cutover lock is held") from error
            yield
        finally:
            os.close(descriptor)

    def load(self) -> CutoverReceipt | None:
        if not self._path.exists():
            return None
        return parse_receipt(_read_receipt(self._path))

    def write(self, receipt: CutoverReceipt) -> None:
        payload = canonical_receipt_bytes(receipt)
        parse_receipt(payload)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        temporary = self._root / f".{_RECEIPT_NAME}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._path)
        _fsync_directory(self._root)

def _read_receipt(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OpenCodeGoCutoverError("Cutover receipt must be a mode-0600 regular file")
        if metadata.st_size > _MAX_RECEIPT_BYTES:
            raise OpenCodeGoCutoverError("Cutover receipt exceeds its byte limit")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(raw) != metadata.st_size:
        raise OpenCodeGoCutoverError("Cutover receipt was not read exactly")
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
