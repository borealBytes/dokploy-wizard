"""Verify and apply the ordered Task 7 K-Dense patch set."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

_PATCH_NAMES = ("kdense-pricing-cache.patch", "kdense-central-only.patch")


def verify_kdense_patch_files(
    repository_root: Path, contract: KdenseRuntimeContract
) -> tuple[Path, ...]:
    """Require the plan inputs and committed copies to have exact ordered bytes."""

    paths: list[Path] = []
    for patch in contract.patches:
        committed = repository_root / patch.path
        planned = repository_root / ".omo" / "plans" / "assets" / f"kdense-{patch.name}.patch"
        if _sha256(committed) != patch.sha256 or _sha256(planned) != patch.sha256:
            raise RuntimeAssetError("K-Dense patch checksum is invalid")
        if committed.read_bytes() != planned.read_bytes():
            raise RuntimeAssetError("K-Dense committed patch copy is invalid")
        paths.append(committed)
    return tuple(paths)


def apply_kdense_patches(source_root: Path, patches: tuple[Path, ...]) -> None:
    """Dry-run then apply every patch in the required sequential order."""

    if tuple(patch.name for patch in patches) != _PATCH_NAMES:
        raise RuntimeAssetError("K-Dense patch order is invalid")
    for patch in patches:
        _run_patch(source_root, patch, dry_run=True)
        _run_patch(source_root, patch, dry_run=False)


def _run_patch(source_root: Path, patch: Path, *, dry_run: bool) -> None:
    command = ["patch", "--batch", "--forward", "-p1", "--input", str(patch)]
    if dry_run:
        command.insert(1, "--dry-run")
    try:
        result = subprocess.run(command, cwd=source_root, check=False, capture_output=True)
    except OSError as error:
        raise RuntimeAssetError("K-Dense patch utility is unavailable") from error
    if result.returncode != 0:
        raise RuntimeAssetError("K-Dense patch order is invalid")


def _sha256(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as error:
        raise RuntimeAssetError("K-Dense patch is unreadable") from error
