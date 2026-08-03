#!/usr/bin/env python3
"""Validate and atomically activate an uploaded Git archive without imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Protocol

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class ReadableBytes(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class WritableBytes(Protocol):
    def write(self, data: bytes) -> int: ...


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bootstrap-sha256", required=True)
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument("--active-link", type=Path, required=True)
    args = parser.parse_args()
    _require_hash(Path(__file__), args.bootstrap_sha256)
    archive_sha256, files = _manifest(args.manifest)
    _require_hash(args.archive, archive_sha256)
    _validate_archive(args.archive, files)
    generation = args.releases_root / archive_sha256
    staging = args.releases_root / f".{archive_sha256}.staging"
    args.releases_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if generation.exists() or generation.is_symlink():
        _validate_tree(generation, files)
    else:
        if staging.exists() or staging.is_symlink():
            _validate_tree(staging, files)
        else:
            _extract(args.archive, files, staging)
        os.replace(staging, generation)
        _fsync(args.releases_root)
    temporary = args.active_link.with_name(f".{args.active_link.name}.{generation.name}.next")
    args.active_link.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("activation temporary link exists")
    os.symlink(generation, temporary)
    os.replace(temporary, args.active_link)
    _fsync(args.active_link.parent)
    return 0


def _manifest(path: Path) -> tuple[str, list[object]]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"archive_sha256", "commit_sha", "files"}:
        raise ValueError("invalid manifest")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical or not isinstance(value["files"], list):
        raise ValueError("noncanonical manifest")
    archive_sha256 = value["archive_sha256"]
    commit_sha = value["commit_sha"]
    if (
        not isinstance(archive_sha256, str)
        or _SHA256.fullmatch(archive_sha256) is None
        or not isinstance(commit_sha, str)
        or _COMMIT.fullmatch(commit_sha) is None
    ):
        raise ValueError("invalid manifest")
    return archive_sha256, value["files"]


def _validate_archive(archive: Path, files: object) -> None:
    expected = _entries(files)
    found: dict[str, tuple[str, int, int]] = {}
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            _path(member.name.rstrip("/"))
            if member.isdir():
                continue
            if not member.isreg() or member.name in found:
                raise ValueError("unsafe archive member")
            stream = handle.extractfile(member)
            if stream is None:
                raise ValueError("unreadable archive member")
            with stream:
                digest = _digest_stream(stream, member.size)
            found[member.name] = (digest, member.size, member.mode & 0o777)
    if found != expected:
        raise ValueError("archive does not match manifest")


def _extract(archive: Path, files: object, target: Path) -> None:
    target.mkdir(mode=0o700)
    expected = _entries(files)
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            if member.isdir():
                continue
            digest, size, mode = expected[member.name]
            destination = target.joinpath(*PurePosixPath(member.name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            stream = handle.extractfile(member)
            if stream is None:
                raise ValueError("unreadable archive member")
            with stream, destination.open("xb") as output:
                actual = _copy(stream, output, size)
                output.flush()
                os.fsync(output.fileno())
            if actual != digest:
                raise ValueError("archive changed during extraction")
            os.chmod(destination, mode)
    _validate_tree(target, files)
    _fsync(target)


def _validate_tree(root: Path, files: object) -> None:
    expected = _entries(files)
    actual: dict[str, tuple[str, int, int]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("unsafe generation entry")
        actual[relative] = (_digest_file(path), metadata.st_size, metadata.st_mode & 0o777)
    if actual != expected:
        raise ValueError("generation does not match manifest")


def _entries(value: object) -> dict[str, tuple[str, int, int]]:
    if not isinstance(value, list):
        raise ValueError("invalid manifest entries")
    result: dict[str, tuple[str, int, int]] = {}
    paths: list[str] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"mode", "path", "sha256", "size"}:
            raise ValueError("invalid manifest entry")
        name, digest, size, mode = item["path"], item["sha256"], item["size"], item["mode"]
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(mode, int)
            or isinstance(mode, bool)
        ):
            raise ValueError("invalid manifest entry")
        _path(name)
        if (
            _SHA256.fullmatch(digest) is None
            or size < 0
            or not 0 <= mode <= 0o777
            or name in result
        ):
            raise ValueError("invalid manifest entry")
        result[name] = (digest, size, mode)
        paths.append(name)
    if not result or paths != sorted(paths):
        raise ValueError("manifest entries are not canonical")
    return result


def _path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("unsafe path")


def _copy(source: ReadableBytes, output: WritableBytes, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = source.read(min(65536, remaining))
        if not chunk:
            raise ValueError("truncated member")
        output.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _digest_stream(stream: ReadableBytes, size: int) -> str:
    return _copy(stream, _NullWriter(), size)


class _NullWriter:
    def write(self, _data: bytes) -> int:
        return len(_data)


def _digest_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _digest_stream(handle, path.stat().st_size)


def _require_hash(path: Path, expected: str) -> None:
    if len(expected) != 64 or _digest_file(path) != expected:
        raise ValueError("transfer hash mismatch")


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
