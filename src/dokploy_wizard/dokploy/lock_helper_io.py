"""Bounded fsynced JSON I/O for the copied lock helper runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.lock_helper_protocol import JsonValue
elif __package__:
    from dokploy_wizard.dokploy.lock_helper_protocol import JsonValue
else:
    from lock_helper_protocol import JsonValue


def atomic_json(path: Path, payload: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path: Path) -> dict[str, JsonValue]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > 256 * 1024:
            raise RuntimeError("helper document is oversized")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(raw) != metadata.st_size:
        raise RuntimeError("helper document size changed")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("helper document must be an object")
    return payload
