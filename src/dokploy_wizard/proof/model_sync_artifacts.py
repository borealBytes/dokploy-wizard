from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

from dokploy_wizard.proof import CaptureSchemaError as CaptureSchemaError
from dokploy_wizard.proof import JsonScalar as JsonScalar
from dokploy_wizard.proof import JsonValue as JsonValue
from dokploy_wizard.proof import ProtectedManifestEntry as ProtectedManifestEntry
from dokploy_wizard.proof import ResourcePlaneCapture as ResourcePlaneCapture
from dokploy_wizard.proof import _redact_mapping
from dokploy_wizard.proof import normalize_image_repository as normalize_image_repository
from dokploy_wizard.proof import parse_resource_planes as parse_resource_planes
from dokploy_wizard.proof import protected_manifest_bytes as protected_manifest_bytes
from dokploy_wizard.proof import redact_manifest_value as redact_manifest_value
from dokploy_wizard.proof import registry_manifest_digest as registry_manifest_digest
from dokploy_wizard.proof import require_digest as require_digest
from dokploy_wizard.proof import require_keys as require_keys
from dokploy_wizard.proof import require_list as require_list
from dokploy_wizard.proof import require_mapping as require_mapping
from dokploy_wizard.proof import require_safe_base_url as require_safe_base_url
from dokploy_wizard.proof import require_sha256 as require_sha256
from dokploy_wizard.proof import require_text as require_text
from dokploy_wizard.proof import (
    validate_protected_manifest_bytes as validate_protected_manifest_bytes,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        temporary_created = True
        try:
            _write_all(descriptor, content)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        temporary_created = False
        _fsync_directory(parent)
    except BaseException:
        if temporary_created:
            try:
                temporary.unlink(missing_ok=True)
            except BaseException:
                pass
        raise


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_protected_manifest(path: Path, payload: Mapping[str, JsonValue]) -> str:
    encoded = (
        json.dumps(_redact_mapping(payload), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)
    return sha256_bytes(encoded)


def read_protected_manifest(path: Path) -> dict[str, JsonValue]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("protected manifest must be a JSON object")
    return _redact_mapping(decoded)


def _artifact_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _artifact_identity(value: os.stat_result) -> tuple[int, ...]:
    return _artifact_metadata(value)[:-1]


def _inspect_exact_regular_bytes(path: Path, expected: bytes) -> os.stat_result | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CaptureSchemaError("artifact output is unreadable") from error
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError as error:
        raise CaptureSchemaError("artifact output is unreadable") from error
    try:
        descriptor_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_before.st_mode)
            or stat.S_IMODE(descriptor_before.st_mode) != 0o600
            or _artifact_metadata(before) != _artifact_metadata(descriptor_before)
        ):
            raise CaptureSchemaError("artifact output must be a mode-0600 regular file")
        limit = len(expected) + 1
        content = bytearray()
        while len(content) < limit:
            remaining = limit - len(content)
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            if len(chunk) > remaining:
                raise CaptureSchemaError("artifact output bytes are not authorized")
            content.extend(chunk)
        descriptor_after = os.fstat(descriptor)
        try:
            pathname_after = os.lstat(path)
        except OSError as error:
            raise CaptureSchemaError("artifact output changed during inspection") from error
        if _artifact_metadata(descriptor_before) != _artifact_metadata(
            descriptor_after
        ) or _artifact_metadata(descriptor_after) != _artifact_metadata(pathname_after):
            raise CaptureSchemaError("artifact output changed during inspection")
        if bytes(content) != expected:
            raise CaptureSchemaError("artifact output bytes are not authorized")
        return descriptor_after
    finally:
        os.close(descriptor)


def read_exact_regular_bytes(path: Path, expected: bytes) -> bool:
    """Return false for an absent path and reject every non-exact present pathname."""
    return _inspect_exact_regular_bytes(path, expected) is not None


def write_or_verify_exact_bytes(path: Path, expected: bytes) -> None:
    if read_exact_regular_bytes(path, expected):
        return
    staging = path.parent / f".{path.name}.{secrets.token_hex(12)}.create"
    atomic_write_bytes(staging, expected)
    try:
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError:
            if not read_exact_regular_bytes(path, expected):
                raise CaptureSchemaError("artifact output changed during creation")
        staging.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        try:
            staging.unlink(missing_ok=True)
        except BaseException:
            pass
        raise


def unlink_exact_regular_bytes(path: Path, expected: bytes) -> None:
    authorized = _inspect_exact_regular_bytes(path, expected)
    if authorized is None:
        return
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    quarantine = f".{path.name}.{secrets.token_hex(12)}.unlink"
    quarantine_is_placeholder = False
    try:
        placeholder_descriptor = os.open(
            quarantine,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        quarantine_is_placeholder = True
        os.close(placeholder_descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _artifact_metadata(current) != _artifact_metadata(authorized):
            raise CaptureSchemaError("artifact output changed before unlink")
        os.rename(
            path.name,
            quarantine,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        quarantine_is_placeholder = False
        moved = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
        if _artifact_identity(moved) != _artifact_identity(authorized):
            try:
                os.link(
                    quarantine,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                pass
            else:
                os.unlink(quarantine, dir_fd=parent_descriptor)
            raise CaptureSchemaError("artifact output changed before unlink")
        os.unlink(quarantine, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if quarantine_is_placeholder:
            try:
                os.unlink(quarantine, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)
