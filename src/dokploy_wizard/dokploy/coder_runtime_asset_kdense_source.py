"""Verify the immutable K-Dense archive against its Task 7 attestation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_archives import extract_runtime_archive
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import (
    KdenseRuntimeContract,
    KdenseSourcePath,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError


@dataclass(frozen=True, slots=True)
class KdenseArchiveEvidence:
    """The verified required source records and absent-lock inventory."""

    source_paths: tuple[KdenseSourcePath, ...]
    uv_lock_paths: tuple[str, ...]


def verify_kdense_source_archive(
    archive_path: Path, contract: KdenseRuntimeContract
) -> KdenseArchiveEvidence:
    """Safely extract and verify the pinned K-Dense source layout and file hashes."""

    if _sha256_file(archive_path) != contract.archive_sha256:
        raise RuntimeAssetError("K-Dense source archive checksum is invalid")
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-kdense-source-") as temporary:
        root = Path(temporary) / "source"
        extract_runtime_archive(
            archive_path=archive_path,
            destination=root,
            expected_root=contract.archive_root,
        )
        evidence = _source_evidence(root, contract)
    return evidence


def verify_canonical_kdense_manifest(
    manifest_path: Path, archive_path: Path, contract: KdenseRuntimeContract
) -> None:
    """Require a canonical checked-in manifest whose K-Dense inputs match the archive."""

    verify_kdense_source_archive(archive_path, contract)
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeAssetError("runtime manifest is unreadable") from error
    canonical = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if raw != canonical:
        raise RuntimeAssetError("runtime manifest is not canonical")


def _source_evidence(root: Path, contract: KdenseRuntimeContract) -> KdenseArchiveEvidence:
    if (root / "pyproject.toml").exists():
        raise RuntimeAssetError("K-Dense root Python project is invalid")
    if (root / "prep_sandbox.py").exists():
        raise RuntimeAssetError("K-Dense stale Python prep entrypoint is present")
    uv_locks = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("uv.lock")))
    if uv_locks:
        raise RuntimeAssetError("K-Dense upstream uv lock inventory is invalid")
    observed = tuple(_source_path(root, path) for path in contract.source_paths)
    if observed != contract.source_paths:
        raise RuntimeAssetError("K-Dense required source path hash is invalid")
    return KdenseArchiveEvidence(source_paths=observed, uv_lock_paths=uv_locks)


def _source_path(root: Path, expected: KdenseSourcePath) -> KdenseSourcePath:
    path = root / expected.path
    if not path.is_file() or path.is_symlink():
        raise RuntimeAssetError("K-Dense required source path is invalid")
    return KdenseSourcePath(path=expected.path, sha256=_sha256_file(path))


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as error:
        raise RuntimeAssetError("K-Dense source file is unreadable") from error
