"""Exact Task 1 legacy evidence adoption for explicit workspace updates."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_adoption_shapes import (
    LegacyShapeBinding,
    validate_json_shape,
    validate_yaml_shape,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_cleanup import write_adoption
from dokploy_wizard.dokploy.workspace_catalog_sync_contracts import (
    require_internal_base_url,
    require_sha256,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_io import sha256, target_snapshot
from dokploy_wizard.dokploy.workspace_catalog_sync_json import (
    json_document_sha,
    json_pointer_sha,
    json_value_at,
    json_value_sha,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    LegacyAdoptionBinding,
    LegacyAdoptionReceipt,
    LegacyAdoptionRequest,
    LegacyPointerEvidence,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import read_regular
from dokploy_wizard.dokploy.workspace_catalog_sync_transition import timestamp
from dokploy_wizard.dokploy.workspace_catalog_sync_yaml import (
    yaml_pointer_sha,
    yaml_value_at,
)


def adopt_legacy(
    workspace_root: Path, transaction_dir: Path, request: LegacyAdoptionRequest
) -> LegacyAdoptionReceipt:
    evidence = request.evidence
    _validate_binding(evidence, request.binding)
    require_sha256(request.current_pre_sha256, "workspace current pointer prehash")
    require_sha256(request.current_target_sha256, "workspace current target prehash")
    target = _workspace_path(workspace_root, evidence.target)
    before = target_snapshot(target, trusted_root=workspace_root)
    if (
        before.pre_state != "file"
        or before.pre_mode != evidence.mode
        or before.pre_sha256 != request.current_target_sha256
    ):
        raise TransactionBlockedError("workspace legacy target no longer matches evidence")
    content = read_regular(
        target,
        2 * 1024 * 1024,
        trusted_root=workspace_root,
        private=False,
    )
    current_pointer_sha = _current_pointer_sha(workspace_root, content, evidence)
    after = target_snapshot(target, trusted_root=workspace_root)
    if before != after:
        raise TransactionBlockedError("workspace legacy target changed during adoption")
    if (
        current_pointer_sha != request.current_pre_sha256
        or current_pointer_sha != evidence.pointer_sha256
        or evidence.pointer_sha256 != evidence.legacy_renderer_sha256
    ):
        raise TransactionBlockedError("workspace legacy pointer no longer matches evidence")
    receipt = LegacyAdoptionReceipt(
        schema_version=1,
        workspace_id=evidence.workspace_id,
        template_id=evidence.template_id,
        template_version_id=evidence.template_version_id,
        target=evidence.target,
        pointer=evidence.pointer,
        baseline_sha256=evidence.legacy_renderer_sha256,
        current_pre_sha256=request.current_pre_sha256,
        credential_value_sha256=evidence.credential_value_sha256,
        legacy_renderer_sha256=evidence.legacy_renderer_sha256,
        adopted_at=timestamp(),
    )
    write_adoption(transaction_dir, receipt, trusted_root=workspace_root)
    return receipt


def _validate_binding(evidence: LegacyPointerEvidence, binding: LegacyAdoptionBinding) -> None:
    identities = (
        evidence.workspace_id,
        evidence.template_id,
        evidence.template_version_id,
        evidence.target,
        evidence.pointer,
        evidence.mode,
        evidence.shape,
        evidence.scope,
        evidence.base_url,
        evidence.credential_value_sha256,
    )
    expected = (
        binding.workspace_id,
        binding.template_id,
        binding.template_version_id,
        binding.target,
        binding.pointer,
        binding.mode,
        binding.shape,
        binding.scope,
        binding.base_url,
        binding.credential_value_sha256,
    )
    if (
        identities != expected
        or any(not value for value in identities)
        or not evidence.legacy_exact
    ):
        raise TransactionBlockedError("workspace legacy evidence does not match update binding")
    if len(evidence.mode) != 4 or any(character not in "01234567" for character in evidence.mode):
        raise TransactionBlockedError("workspace legacy mode is invalid")
    if (evidence.scope, evidence.shape) not in {
        ("pointer", "json-pointer"),
        ("pointer", "yaml-pointer"),
        ("target-and-symlink", "json-target-and-symlink"),
    }:
        raise TransactionBlockedError("workspace legacy structural shape is invalid")
    try:
        require_internal_base_url(evidence.base_url)
        require_sha256(evidence.credential_value_sha256, "workspace legacy credential hash")
        require_sha256(evidence.pointer_sha256, "workspace legacy pointer hash")
        require_sha256(evidence.legacy_renderer_sha256, "workspace legacy renderer hash")
    except WorkspaceCatalogSyncError as error:
        raise TransactionBlockedError(str(error)) from error


def _current_pointer_sha(
    workspace_root: Path, content: bytes, evidence: LegacyPointerEvidence
) -> str:
    binding = LegacyShapeBinding(
        base_url=evidence.base_url,
        credential_value_sha256=evidence.credential_value_sha256,
    )
    match evidence.shape:
        case "json-pointer":
            value = json_value_at(content, evidence.pointer)
            validate_json_shape(value, evidence.pointer, binding)
            return json_pointer_sha(content, evidence.pointer)
        case "yaml-pointer":
            pointer = _yaml_pointer(evidence.pointer)
            value = yaml_value_at(content, pointer)
            validate_yaml_shape(value, evidence.pointer, binding)
            return yaml_pointer_sha(content, pointer)
        case "json-target-and-symlink":
            return _kdense_pointer_sha(workspace_root, content, evidence)
        case unreachable:
            assert_never(unreachable)


def _kdense_pointer_sha(
    workspace_root: Path, content: bytes, evidence: LegacyPointerEvidence
) -> str:
    if (
        evidence.target_sha256 is None
        or evidence.symlink_state != "present"
        or evidence.symlink_target is None
        or evidence.symlink_sha256 is None
        or evidence.renderer_source_path != "web/src/data/models.json"
        or evidence.renderer_source_revision is None
        or len(evidence.renderer_source_revision) != 40
        or any(
            character not in "0123456789abcdef" for character in evidence.renderer_source_revision
        )
    ):
        raise TransactionBlockedError("workspace legacy K-Dense evidence is incomplete")
    target_sha = json_document_sha(content)
    require_sha256(evidence.target_sha256, "workspace legacy K-Dense target hash")
    require_sha256(evidence.symlink_sha256, "workspace legacy K-Dense symlink hash")
    current = _workspace_path(workspace_root, evidence.pointer)
    try:
        metadata = os.lstat(current)
    except FileNotFoundError as error:
        raise TransactionBlockedError(
            "workspace legacy K-Dense current target is absent"
        ) from error
    if not stat.S_ISLNK(metadata.st_mode):
        raise TransactionBlockedError("workspace legacy K-Dense current target is not a symlink")
    symlink_target = os.readlink(current)
    symlink_sha = sha256(symlink_target.encode())
    if (
        target_sha != evidence.target_sha256
        or symlink_target != evidence.symlink_target
        or symlink_sha != evidence.symlink_sha256
    ):
        raise TransactionBlockedError("workspace legacy K-Dense target or symlink changed")
    return json_value_sha(
        {
            "base_url": evidence.base_url,
            "credential_value_sha256": evidence.credential_value_sha256,
            "symlink_sha256": symlink_sha,
            "target_sha256": target_sha,
        }
    )


def _workspace_path(workspace_root: Path, raw: str) -> Path:
    root = workspace_root.resolve(strict=True)
    path = Path(raw)
    absolute = path if path.is_absolute() else root / path
    try:
        absolute.parent.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise TransactionBlockedError("workspace legacy target escapes workspace") from error
    return absolute


def _yaml_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or "~" in pointer:
        raise TransactionBlockedError("workspace legacy YAML pointer is invalid")
    segments = tuple(pointer[1:].split("/"))
    if not segments or any(not segment for segment in segments):
        raise TransactionBlockedError("workspace legacy YAML pointer is invalid")
    return segments
