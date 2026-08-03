"""Build and validate architecture-specific immutable workspace image IDs."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dokploy_wizard.dokploy.coder_runtime_asset_manifest import runtime_manifest_path
from dokploy_wizard.dokploy.coder_runtime_asset_materializer import materialize_runtime_assets
from dokploy_wizard.dokploy.coder_runtime_asset_templates import validate_derived_image
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    ARCHITECTURES,
    DerivedImageInspection,
    JsonValue,
    RuntimeAssetError,
    RuntimeManifest,
    require_mapping,
    require_text,
)


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeImages:
    """Full image IDs selected for Coder workspace provisioners."""

    amd64: str
    arm64: str


class WorkspaceRuntimeImageAdapter(Protocol):
    """Build or inspect one architecture-specific workspace runtime image."""

    def ensure_image(
        self, *, manifest: RuntimeManifest, architecture: str
    ) -> DerivedImageInspection: ...


def build_workspace_runtime_images(
    manifest: RuntimeManifest, adapter: WorkspaceRuntimeImageAdapter
) -> WorkspaceRuntimeImages:
    """Build or verify both runtime architectures before template rendering."""

    inspections = {
        architecture: adapter.ensure_image(manifest=manifest, architecture=architecture)
        for architecture in sorted(ARCHITECTURES)
    }
    image_ids = {
        architecture: validate_derived_image(manifest, inspection)
        for architecture, inspection in inspections.items()
    }
    return WorkspaceRuntimeImages(amd64=image_ids["amd64"], arm64=image_ids["arm64"])


@dataclass(frozen=True, slots=True)
class DockerWorkspaceRuntimeImageAdapter:
    """Materialize and build the locked runtime image through the local Docker daemon."""

    repository_root: Path

    def ensure_image(
        self, *, manifest: RuntimeManifest, architecture: str
    ) -> DerivedImageInspection:
        """Return a validated existing image or build it with networking disabled."""

        if architecture not in ARCHITECTURES:
            raise RuntimeAssetError("runtime architecture is invalid")
        tag = manifest.derived_image_tag(architecture)
        existing = _inspect_image(tag, architecture)
        if existing is not None:
            return existing
        with tempfile.TemporaryDirectory(prefix="dokploy-wizard-runtime-image-") as temporary:
            context = Path(temporary)
            _stage_build_context(self.repository_root, context, architecture)
            command = (
                "docker",
                "build",
                "--network=none",
                "--platform",
                f"linux/{architecture}",
                "--tag",
                tag,
                "--build-arg",
                f"TARGETARCH={architecture}",
                "--build-arg",
                f"RUNTIME_MANIFEST_SHA256={manifest.lock_sha256}",
                "--build-arg",
                f"SOURCE_DATE_EPOCH={manifest.source_date_epoch}",
                "--file",
                str(context / "templates" / "coder" / "runtime" / "Dockerfile"),
                str(context),
            )
            _run_docker(command)
        inspection = _inspect_image(tag, architecture)
        if inspection is None:
            raise RuntimeAssetError("derived image inspection is unavailable")
        return inspection


def _stage_build_context(source_root: Path, context: Path, architecture: str) -> None:
    runtime_source = source_root / "templates" / "coder" / "runtime"
    runtime_destination = context / "templates" / "coder" / "runtime"
    manifest_destination = context / "src" / "dokploy_wizard" / "runtime-manifest.lock.json"
    runtime_destination.mkdir(parents=True)
    (runtime_destination / "assets").mkdir()
    manifest_destination.parent.mkdir(parents=True)
    shutil.copyfile(runtime_manifest_path(), manifest_destination)
    for name in ("Dockerfile", "verify-runtime-assets"):
        shutil.copyfile(runtime_source / name, runtime_destination / name)
    materialize_runtime_assets(
        manifest_path=runtime_manifest_path(),
        architecture=architecture,
        output_dir=runtime_destination / "assets" / architecture,
    )


def _inspect_image(tag: str, architecture: str) -> DerivedImageInspection | None:
    result = subprocess.run(
        ("docker", "image", "inspect", "--format", "{{json .}}", tag),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        payload: JsonValue = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeAssetError("derived image inspection is invalid") from error
    document = require_mapping(payload, "derived image inspection")
    config = require_mapping(document.get("Config"), "derived image config")
    labels = require_mapping(config.get("Labels"), "derived image labels")
    rootfs = require_mapping(document.get("RootFS"), "derived image rootfs")
    layers = rootfs.get("Layers")
    if not isinstance(layers, list):
        raise RuntimeAssetError("derived image layers are invalid")
    label_values = {
        key: require_text(value, "derived image label") for key, value in labels.items()
    }
    return DerivedImageInspection(
        architecture=architecture,
        tag=tag,
        image_id=require_text(document.get("Id"), "derived image ID"),
        labels=label_values,
        layers=tuple(require_text(layer, "derived image layer") for layer in layers),
    )


def _run_docker(command: tuple[str, ...]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeAssetError("derived image build failed")
