"""Atomic activation of exact content-addressed release generations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.release_manifest import (
    ReleaseError,
    ReleaseManifest,
    extract_release,
    manifest_from_archive,
    validate_release_tree,
)


@dataclass(frozen=True, slots=True)
class ReleaseActivation:
    """Value-free record of an atomically selected release generation."""

    archive_sha256: str
    manifest_sha256: str
    generation_path: Path


def activate_release(
    *, archive_path: Path, manifest: ReleaseManifest, releases_root: Path, active_link: Path
) -> ReleaseActivation:
    """Activate only a newly extracted or exactly resumed release generation."""

    if manifest_from_archive(manifest.commit_sha, archive_path) != manifest:
        raise ReleaseError("release archive does not match its manifest")
    releases_root.mkdir(parents=True, exist_ok=True)
    _fsync_directory(releases_root.parent)
    generation = releases_root / manifest.archive_sha256
    staging = releases_root / f".{manifest.archive_sha256}.staging"
    if generation.exists() or generation.is_symlink():
        validate_release_tree(generation, manifest)
    else:
        if staging.exists() or staging.is_symlink():
            validate_release_tree(staging, manifest)
        else:
            extract_release(archive_path, manifest, staging)
        try:
            os.replace(staging, generation)
        except OSError as error:
            raise ReleaseError("release generation publication failed") from error
        _fsync_directory(releases_root)
    _switch_active_generation(active_link, generation)
    return ReleaseActivation(manifest.archive_sha256, manifest.sha256, generation)


def _switch_active_generation(active_link: Path, generation: Path) -> None:
    active_link.parent.mkdir(parents=True, exist_ok=True)
    temporary = active_link.with_name(f".{active_link.name}.{generation.name}.next")
    if temporary.exists() or temporary.is_symlink():
        if not temporary.is_symlink() or Path(os.readlink(temporary)) != generation:
            raise ReleaseError("release activation temporary link drifted")
        temporary.unlink()
        _fsync_directory(active_link.parent)
    try:
        os.symlink(generation, temporary)
        os.replace(temporary, active_link)
    except OSError as error:
        raise ReleaseError("release activation switch failed") from error
    _fsync_directory(active_link.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
