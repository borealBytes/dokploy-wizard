"""Validate Coder templates against immutable runtime image contracts."""

from __future__ import annotations

import re
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_manifest import (
    load_runtime_manifest,
    runtime_manifest_path,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    ARCHITECTURES,
    DerivedImageInspection,
    RuntimeAssetError,
    RuntimeManifest,
    require_sha256,
)


def validate_template_runtime_inputs(paths: tuple[Path, ...]) -> None:
    """Require templates to consume persisted Task 6 workspace image IDs."""

    manifest = load_runtime_manifest(runtime_manifest_path())
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeAssetError("runtime template is unreadable") from error
        if manifest.workspace_base_image in text or not _uses_persisted_workspace_image(text):
            raise RuntimeAssetError(
                "runtime template must select derived runtime images through "
                "persisted inspected IDs"
            )


def validate_derived_image(manifest: RuntimeManifest, inspection: DerivedImageInspection) -> str:
    """Validate build labels and return the full inspected runtime image ID."""

    if inspection.architecture not in ARCHITECTURES:
        raise RuntimeAssetError("derived image architecture is invalid")
    if inspection.tag != manifest.derived_image_tag(inspection.architecture):
        raise RuntimeAssetError("derived image tag is invalid")
    if not inspection.image_id.startswith("sha256:"):
        raise RuntimeAssetError("derived image ID is invalid")
    require_sha256(inspection.image_id.removeprefix("sha256:"), "derived image ID")
    expected_labels = {
        "org.opencontainers.image.base.name": manifest.workspace_base_image,
        "org.opencontainers.image.revision": manifest.lock_sha256,
        "org.opencontainers.image.created": str(manifest.source_date_epoch),
    }
    if inspection.labels != expected_labels:
        raise RuntimeAssetError("derived image label is invalid")
    if not inspection.layers:
        raise RuntimeAssetError("derived image layers are missing")
    for layer in inspection.layers:
        if not layer.startswith("sha256:"):
            raise RuntimeAssetError("derived image layer is invalid")
        require_sha256(layer.removeprefix("sha256:"), "derived image layer")
    return inspection.image_id


def _uses_persisted_workspace_image(text: str) -> bool:
    return (
        'resource "docker_image" "workspace"' in text
        and re.search(r"\bname\s*=\s*local\.runtime_image", text) is not None
        and re.search(r"\bimage\s*=\s*docker_image\.workspace\.image_id", text) is not None
        and re.search(r"\bimage\s*=\s*local\.runtime_image", text) is None
    )
