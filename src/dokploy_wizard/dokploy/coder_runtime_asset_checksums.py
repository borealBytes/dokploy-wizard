"""Deterministic integrity manifests for offline Coder runtime payloads."""

from __future__ import annotations

import hashlib
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

_CHECKSUMS_NAME = "checksums.sha256"
_LINKS_NAME = "links.sha256"


def write_materialized_checksums(root: Path) -> None:
    """Write complete regular-file and symlink-projection integrity manifests."""

    (root / _CHECKSUMS_NAME).write_text(_regular_manifest(root), encoding="utf-8")
    (root / _LINKS_NAME).write_text(_link_manifest(root), encoding="utf-8")


def verify_materialized_tree(root: Path) -> None:
    """Reject a materialized payload whose regular files or links drifted."""

    if _read_manifest(root / _CHECKSUMS_NAME) != _records(root, regular=True):
        raise RuntimeAssetError("runtime asset checksum verification failed")
    if _read_manifest(root / _LINKS_NAME) != _records(root, regular=False):
        raise RuntimeAssetError("runtime asset link checksum verification failed")


def _regular_manifest(root: Path) -> str:
    return _manifest_lines(_records(root, regular=True))


def _link_manifest(root: Path) -> str:
    return _manifest_lines(_records(root, regular=False))


def _records(root: Path, *, regular: bool) -> frozenset[tuple[str, str]]:
    records: set[tuple[str, str]] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative in {_CHECKSUMS_NAME, _LINKS_NAME}:
            continue
        if regular and path.is_file() and not path.is_symlink():
            records.add((_sha256(path.read_bytes()), relative))
        if not regular and path.is_symlink():
            records.add((_sha256(path.readlink().as_posix().encode()), relative))
    return frozenset(records)


def _read_manifest(path: Path) -> frozenset[tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeAssetError("runtime asset checksum manifest is unreadable") from error
    records: set[tuple[str, str]] = set()
    for line in lines:
        checksum, separator, relative = line.partition("  ")
        if separator != "  " or len(checksum) != 64 or not relative:
            raise RuntimeAssetError("runtime asset checksum manifest is invalid")
        records.add((checksum, relative))
    if len(records) != len(lines):
        raise RuntimeAssetError("runtime asset checksum manifest is invalid")
    return frozenset(records)


def _manifest_lines(records: frozenset[tuple[str, str]]) -> str:
    return "".join(f"{checksum}  {relative}\n" for checksum, relative in sorted(records))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
