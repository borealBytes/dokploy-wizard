"""Content-addressed release manifest construction and validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MAX_ARCHIVE_BYTES: Final = 256 * 1024 * 1024
_MAX_FILE_BYTES: Final = 64 * 1024 * 1024


class ReleaseError(RuntimeError):
    """Raised when an immutable release cannot be proven safe to activate."""


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    """One regular file in an immutable release tree."""

    path: str
    sha256: str
    size: int
    mode: int

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        if (
            _SHA256.fullmatch(self.sha256) is None
            or self.size < 0
            or not 0 <= self.mode <= 0o777
        ):
            raise ReleaseError("release manifest file entry is invalid")

    def to_dict(self) -> dict[str, int | str]:
        return {"mode": self.mode, "path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """Exact value-free identity of the commit archive release tree."""

    commit_sha: str
    archive_sha256: str
    files: tuple[ReleaseFile, ...]

    def __post_init__(self) -> None:
        if (
            _COMMIT.fullmatch(self.commit_sha) is None
            or _SHA256.fullmatch(self.archive_sha256) is None
        ):
            raise ReleaseError("release manifest identity is invalid")
        paths = tuple(entry.path for entry in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ReleaseError("release manifest paths are not canonical")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def to_bytes(self) -> bytes:
        payload = {
            "archive_sha256": self.archive_sha256,
            "commit_sha": self.commit_sha,
            "files": [entry.to_dict() for entry in self.files],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def manifest_from_archive(commit_sha: str, archive_path: Path) -> ReleaseManifest:
    """Read a bounded regular archive into its deterministic release manifest."""

    _validate_regular_file(archive_path, _MAX_ARCHIVE_BYTES, "release archive")
    archive_sha256 = _sha256_file(archive_path)
    entries: list[ReleaseFile] = []
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    _validate_relative_path(member.name.rstrip("/"))
                    continue
                if not member.isreg() or member.size > _MAX_FILE_BYTES:
                    raise ReleaseError("release archive contains an unsafe member")
                _validate_relative_path(member.name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseError("release archive member cannot be read")
                with stream:
                    digest = hashlib.sha256()
                    remaining = member.size
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise ReleaseError("release archive member is truncated")
                        digest.update(chunk)
                        remaining -= len(chunk)
                entries.append(
                    ReleaseFile(
                        member.name,
                        digest.hexdigest(),
                        member.size,
                        member.mode & 0o777,
                    )
                )
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError("release archive is unreadable") from error
    return ReleaseManifest(
        commit_sha,
        archive_sha256,
        tuple(sorted(entries, key=lambda entry: entry.path)),
    )


def extract_release(archive_path: Path, manifest: ReleaseManifest, target: Path) -> None:
    """Extract one verified archive into a new empty generation directory."""

    if manifest_from_archive(manifest.commit_sha, archive_path) != manifest:
        raise ReleaseError("release archive does not match its manifest")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ReleaseError("release extraction target is not empty") from error
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isreg():
                    continue
                destination = target.joinpath(*PurePosixPath(member.name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseError("release archive member cannot be read")
                with stream, destination.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise ReleaseError("release archive member is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(destination, member.mode & 0o777)
        _fsync_directories(target)
        validate_release_tree(target, manifest)
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError("release extraction failed") from error


def validate_release_tree(root: Path, manifest: ReleaseManifest) -> None:
    """Require a release directory to contain exactly its manifest content."""

    if not root.is_dir() or root.is_symlink():
        raise ReleaseError("release generation is not a directory")
    expected_files = {entry.path: entry for entry in manifest.files}
    expected_directories = {
        parent.as_posix()
        for entry in manifest.files
        for parent in PurePosixPath(entry.path).parents
        if parent != PurePosixPath(".")
    }
    actual_files: dict[str, ReleaseFile] = {}
    actual_directories: set[str] = set()
    for path in root.rglob("*"):
        relative_path = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            actual_directories.add(relative_path)
        elif stat.S_ISREG(metadata.st_mode):
            actual_files[relative_path] = ReleaseFile(
                relative_path,
                _sha256_file(path),
                metadata.st_size,
                metadata.st_mode & 0o777,
            )
        else:
            raise ReleaseError("release generation contains an unsafe entry")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ReleaseError("release generation does not match its manifest")


def write_release_manifest(path: Path, manifest: ReleaseManifest) -> None:
    """Write one value-free manifest before it is uploaded with its archive."""

    path.write_bytes(manifest.to_bytes())
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def read_release_manifest(path: Path) -> ReleaseManifest:
    """Read only canonical manifest bytes from an untrusted transfer path."""

    _validate_regular_file(path, _MAX_FILE_BYTES, "release manifest")
    content = path.read_bytes()
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("release manifest is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {"archive_sha256", "commit_sha", "files"}:
        raise ReleaseError("release manifest schema is invalid")
    raw_files = payload["files"]
    if not isinstance(raw_files, list):
        raise ReleaseError("release manifest files are invalid")
    entries: list[ReleaseFile] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"mode", "path", "sha256", "size"}:
            raise ReleaseError("release manifest file entry is invalid")
        path_value = raw_entry["path"]
        sha256_value = raw_entry["sha256"]
        size_value = raw_entry["size"]
        mode_value = raw_entry["mode"]
        if not isinstance(path_value, str) or not isinstance(sha256_value, str):
            raise ReleaseError("release manifest file entry is invalid")
        if isinstance(size_value, bool) or isinstance(mode_value, bool):
            raise ReleaseError("release manifest file entry is invalid")
        if not isinstance(size_value, int) or not isinstance(mode_value, int):
            raise ReleaseError("release manifest file entry is invalid")
        entries.append(ReleaseFile(path_value, sha256_value, size_value, mode_value))
    archive_sha256 = payload["archive_sha256"]
    commit_sha = payload["commit_sha"]
    if not isinstance(archive_sha256, str) or not isinstance(commit_sha, str):
        raise ReleaseError("release manifest identity is invalid")
    manifest = ReleaseManifest(commit_sha, archive_sha256, tuple(entries))
    if content != manifest.to_bytes():
        raise ReleaseError("release manifest is not canonical")
    return manifest


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError("release path is unsafe")
    if path.as_posix() != value:
        raise ReleaseError("release path is not canonical")


def _validate_regular_file(path: Path, limit: int, label: str) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
        raise ReleaseError(f"{label} is not bounded")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directories(root: Path) -> None:
    directories = [root, *sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True)]
    for directory in directories:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
