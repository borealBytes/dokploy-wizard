"""Owned-pointer compare-and-swap rendering for transaction staging."""

from __future__ import annotations

from dokploy_wizard.dokploy.workspace_catalog_sync_io import sha256
from dokploy_wizard.dokploy.workspace_catalog_sync_json import (
    json_document_sha,
    json_value_at,
    parse_json_pointer,
    patch_json_pointer,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogTarget,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_yaml import (
    patch_yaml_pointer,
    yaml_value_at,
)


def render_owned_target(target: CatalogTarget, current: bytes | None) -> bytes:
    if not target.owned_pointers:
        return target.content
    if len({owned.pointer for owned in target.owned_pointers}) != len(target.owned_pointers):
        raise WorkspaceCatalogSyncError("workspace owned pointers are not unique")
    if target.kind == "symlink":
        return _render_root(target, current, document=False)
    if all(owned.pointer == "/" for owned in target.owned_pointers):
        return _render_root(target, current, document=True)
    if current is None:
        current = b"{}\n" if target.path.suffix != ".yaml" else b""
    if target.path.suffix == ".yaml":
        rendered = current
        for owned in target.owned_pointers:
            pointer = _yaml_pointer(owned.pointer)
            yaml_patch = patch_yaml_pointer(
                rendered, pointer, yaml_value_at(target.content, pointer)
            )
            _require_hashes(
                owned.pre_sha256,
                owned.post_sha256,
                yaml_patch.pre_sha256,
                yaml_patch.post_sha256,
            )
            rendered = yaml_patch.content
        return rendered
    rendered = current
    for owned in target.owned_pointers:
        pointer = parse_json_pointer(owned.pointer)
        json_patch = patch_json_pointer(
            rendered,
            pointer,
            json_value_at(target.content, owned.pointer),
        )
        _require_hashes(
            owned.pre_sha256,
            owned.post_sha256,
            json_patch.pre_sha256,
            json_patch.post_sha256,
        )
        rendered = json_patch.content
    return rendered


def _render_root(target: CatalogTarget, current: bytes | None, *, document: bool) -> bytes:
    if len(target.owned_pointers) != 1 or target.owned_pointers[0].pointer != "/":
        raise WorkspaceCatalogSyncError("workspace root ownership is invalid")
    owned = target.owned_pointers[0]
    pre_sha256 = None
    if current is not None:
        pre_sha256 = json_document_sha(current) if document else sha256(current)
    post_sha256 = json_document_sha(target.content) if document else sha256(target.content)
    _require_hashes(owned.pre_sha256, owned.post_sha256, pre_sha256, post_sha256)
    return target.content


def _require_hashes(
    expected_pre: str | None,
    expected_post: str,
    actual_pre: str | None,
    actual_post: str,
) -> None:
    if expected_pre != actual_pre or expected_post != actual_post:
        raise TransactionBlockedError("workspace owned pointer changed before transaction")


def _yaml_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or "~" in pointer:
        raise WorkspaceCatalogSyncError("workspace YAML pointer is invalid")
    result = tuple(pointer[1:].split("/"))
    if not result or any(not segment for segment in result):
        raise WorkspaceCatalogSyncError("workspace YAML pointer is invalid")
    return result
