"""Static workspace destination authorization before transaction preparation."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogTarget,
    WorkspaceCatalogSyncError,
)


def authorize_target(workspace_root: Path, target: CatalogTarget) -> CatalogTarget:
    transitions.assert_workspace_path(workspace_root, target.path)
    if target.kind == "file" and target.mode & ~0o777:
        raise WorkspaceCatalogSyncError("workspace target mode is invalid")
    if target.kind == "symlink":
        _authorize_symlink(workspace_root, target)
    return target


def _authorize_symlink(workspace_root: Path, target: CatalogTarget) -> None:
    try:
        raw_target = target.content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace symlink target is invalid") from error
    link_target = PurePosixPath(raw_target)
    if link_target.is_absolute() or not link_target.parts or ".." in link_target.parts:
        raise WorkspaceCatalogSyncError("workspace symlink target is invalid")
    linked = target.path.parent.joinpath(*link_target.parts).absolute()
    try:
        linked.relative_to(workspace_root)
    except ValueError as error:
        raise WorkspaceCatalogSyncError("workspace symlink target escapes workspace") from error
