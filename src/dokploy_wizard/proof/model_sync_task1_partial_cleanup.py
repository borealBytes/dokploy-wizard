"""Fail-closed cleanup of receipt-owned partial Task 1 materialization."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    sha256,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
    validate_task1_context_evidence,
)
from dokploy_wizard.proof.model_sync_task1_evidence_validation import (
    read_task1_evidence_file,
)

_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


@dataclass(frozen=True, slots=True)
class _PartialMaterialization:
    directory_metadata: os.stat_result | None
    files: tuple[tuple[Path, bytes], ...]
    backup: bytes | None


def inspect_task1_partial_materialization(
    evidence: Task1ProofContextEvidenceV1, backup_path: Path | None = None
) -> None:
    _inspect_partial(evidence, backup_path)


def cleanup_task1_partial_materialization(
    evidence: Task1ProofContextEvidenceV1, backup_path: Path
) -> None:
    partial = _inspect_partial(evidence, backup_path)
    for path, content in partial.files:
        artifacts.unlink_exact_regular_bytes(path, content)
    if partial.backup is not None:
        artifacts.unlink_exact_regular_bytes(backup_path, partial.backup)
    if partial.directory_metadata is not None:
        _remove_exact_empty_directory(Path(evidence.proof_directory), partial.directory_metadata)


def _inspect_partial(
    evidence: Task1ProofContextEvidenceV1, backup_path: Path | None
) -> _PartialMaterialization:
    validate_task1_context_evidence(evidence)
    bindings = _external_bindings(evidence)
    directory = Path(evidence.proof_directory)
    for expected_name, path, _digest, mode in bindings:
        if path != directory / expected_name or mode != _FILE_MODE:
            raise Task1ProofContextError("Task 1 proof external path is not canonical")
    directory_metadata, files = _inspect_directory(directory, bindings)
    backup = _inspect_backup(evidence, backup_path)
    return _PartialMaterialization(directory_metadata, files, backup)


def _external_bindings(
    evidence: Task1ProofContextEvidenceV1,
) -> tuple[tuple[str, Path, str, int], ...]:
    return (
        (
            "upload.env",
            Path(evidence.upload_env_path),
            evidence.uploaded_env_sha256,
            evidence.upload_env_mode,
        ),
        (
            "task1-proof-context.json",
            Path(evidence.context_path),
            evidence.context_sha256,
            evidence.context_mode,
        ),
        (
            "task1-proof-context.seed",
            Path(evidence.seed_path),
            evidence.seed_sha256,
            evidence.seed_mode,
        ),
        (
            "task1-proof-context.receipt.json",
            Path(evidence.receipt_path),
            evidence.overlay_receipt_sha256,
            evidence.receipt_mode,
        ),
    )


def _inspect_directory(
    directory: Path, bindings: tuple[tuple[str, Path, str, int], ...]
) -> tuple[os.stat_result | None, tuple[tuple[Path, bytes], ...]]:
    try:
        before = os.lstat(directory)
    except FileNotFoundError:
        return None, ()
    except OSError as error:
        raise Task1ProofContextError("Task 1 proof directory is unreadable") from error
    if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != _DIRECTORY_MODE:
        raise Task1ProofContextError("Task 1 proof directory is not a mode-0700 directory")
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Task1ProofContextError("Task 1 proof directory is unreadable") from error
    try:
        opened = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(opened):
            raise Task1ProofContextError("Task 1 proof directory identity changed")
        entries = frozenset(os.listdir(descriptor))
        expected = frozenset(name for name, _path, _digest, _mode in bindings)
        if not entries <= expected:
            raise Task1ProofContextError("Task 1 proof directory contains unexpected files")
        files = tuple(
            (path, _read_expected(path, digest, mode))
            for name, path, digest, mode in bindings
            if name in entries
        )
        after = os.fstat(descriptor)
        pathname = os.lstat(directory)
        if _directory_identity(opened) != _directory_identity(after) or _directory_identity(
            after
        ) != _directory_identity(pathname):
            raise Task1ProofContextError("Task 1 proof directory identity changed")
        return after, files
    finally:
        os.close(descriptor)


def _inspect_backup(
    evidence: Task1ProofContextEvidenceV1, backup_path: Path | None
) -> bytes | None:
    if backup_path is None or not os.path.lexists(backup_path):
        return None
    return _read_expected(backup_path, evidence.source_env_sha256, _FILE_MODE)


def _read_expected(path: Path, digest: str, mode: int) -> bytes:
    content = read_task1_evidence_file(path, mode)
    if sha256(content) != digest:
        raise Task1ProofContextError("Task 1 partial proof file has unknown bytes")
    return content


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
    )


def _remove_exact_empty_directory(path: Path, authorized: os.stat_result) -> None:
    parent_descriptor = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    quarantine = f".{path.name}.{secrets.token_hex(12)}.remove"
    placeholder = False
    moved = False
    try:
        os.mkdir(quarantine, _DIRECTORY_MODE, dir_fd=parent_descriptor)
        placeholder = True
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _directory_identity(current) != _directory_identity(authorized):
            raise Task1ProofContextError("Task 1 proof directory changed before removal")
        os.rename(
            path.name,
            quarantine,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        placeholder = False
        moved = True
        moved_metadata = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
        if _directory_identity(moved_metadata) != _directory_identity(authorized):
            raise Task1ProofContextError("Task 1 proof directory changed before removal")
        moved_descriptor = os.open(
            quarantine,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
        try:
            if os.listdir(moved_descriptor):
                raise Task1ProofContextError("Task 1 proof directory is not empty")
        finally:
            os.close(moved_descriptor)
        os.rmdir(quarantine, dir_fd=parent_descriptor)
        moved = False
        os.fsync(parent_descriptor)
    except BaseException:
        if moved:
            try:
                os.rename(
                    quarantine,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                moved = False
            except OSError:
                pass
        raise
    finally:
        if placeholder:
            try:
                os.rmdir(quarantine, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)
