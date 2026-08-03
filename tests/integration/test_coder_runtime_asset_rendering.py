from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import dokploy_wizard.dokploy.coder as coder_module
import dokploy_wizard.dokploy.coder_runtime_asset_images as images_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.coder_runtime_asset_images import build_workspace_runtime_images
from dokploy_wizard.dokploy.coder_runtime_asset_manifest import (
    load_runtime_manifest,
    runtime_manifest_path,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    DerivedImageInspection,
    RuntimeAssetError,
    RuntimeManifest,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    RawEnvInput,
    ensure_litellm_generated_keys,
    ensure_seaweedfs_generated_secrets,
    ensure_surfsense_generated_secrets,
    load_state_dir,
    parse_env_file,
    persist_install_scaffold,
    resolve_desired_state,
)
from dokploy_wizard.state.runtime_images import RuntimeImages, resolve_runtime_images


def test_runtime_image_ids_round_trip_through_desired_and_applied_state() -> None:
    # Given
    base = resolve_runtime_images({})
    images = RuntimeImages(
        pgvector=base.pgvector,
        redis=base.redis,
        postfix=base.postfix,
        litellm=base.litellm,
        coder=base.coder,
        workspace_amd64="sha256:" + "a" * 64,
        workspace_arm64="sha256:" + "b" * 64,
    )
    source = parse_env_file(Path("fixtures/full.env"))
    raw = RawEnvInput(format_version=1, values={**source.values, "PACKS": "coder"})

    # When
    desired = replace(resolve_desired_state(raw), runtime_images=images)
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=desired.fingerprint(),
        completed_steps=(),
        runtime_images=images,
    )

    # Then
    assert DesiredState.from_dict(desired.to_dict()).runtime_images == images
    assert AppliedStateCheckpoint.from_dict(applied.to_dict()).runtime_images == images


def test_runtime_image_ids_replace_all_template_architecture_tags() -> None:
    # Given
    amd64 = "sha256:" + "a" * 64
    arm64 = "sha256:" + "b" * 64
    templates = tuple(Path("templates/coder").glob("*/main.tf"))

    # When
    replacements = coder_module._workspace_runtime_image_replacements(amd64, arm64)

    # Then
    assert len(templates) == 6
    for template in templates:
        with coder_module._rendered_template_dir(
            template_dir=template.parent, replacements=replacements
        ) as rendered:
            content = (rendered / "main.tf").read_text(encoding="utf-8")
        assert amd64 in content
        assert arm64 in content
        assert "dokploy-wizard/coder-runtime:" not in content


def test_derived_image_adapter_uses_both_architecture_specific_full_ids() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())
    calls: list[str] = []

    class FixtureAdapter:
        def ensure_image(
            self, *, manifest: RuntimeManifest, architecture: str
        ) -> DerivedImageInspection:
            calls.append(architecture)
            return DerivedImageInspection(
                architecture=architecture,
                tag=manifest.derived_image_tag(architecture),
                image_id="sha256:" + ("a" if architecture == "amd64" else "b") * 64,
                labels={
                    "org.opencontainers.image.base.name": manifest.workspace_base_image,
                    "org.opencontainers.image.revision": manifest.lock_sha256,
                    "org.opencontainers.image.created": str(manifest.source_date_epoch),
                },
                layers=("sha256:" + "c" * 64,),
            )

    # When
    images = build_workspace_runtime_images(manifest, FixtureAdapter())

    # Then
    assert calls == ["amd64", "arm64"]
    assert images.amd64 == "sha256:" + "a" * 64
    assert images.arm64 == "sha256:" + "b" * 64


def test_derived_image_adapter_rejects_an_inspection_for_the_wrong_tag() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())

    class MismatchedAdapter:
        def ensure_image(
            self, *, manifest: RuntimeManifest, architecture: str
        ) -> DerivedImageInspection:
            return DerivedImageInspection(
                architecture=architecture,
                tag="dokploy-wizard/coder-runtime:wrong",
                image_id="sha256:" + "a" * 64,
                labels={
                    "org.opencontainers.image.base.name": manifest.workspace_base_image,
                    "org.opencontainers.image.revision": manifest.lock_sha256,
                    "org.opencontainers.image.created": str(manifest.source_date_epoch),
                },
                layers=("sha256:" + "b" * 64,),
            )

    # When / Then
    with pytest.raises(RuntimeAssetError, match="tag"):
        build_workspace_runtime_images(manifest, MismatchedAdapter())


def test_staging_context_creates_the_materializer_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    def materialize(*, manifest_path: Path, architecture: str, output_dir: Path) -> None:
        assert manifest_path == runtime_manifest_path()
        assert architecture == "amd64"
        assert output_dir.parent.is_dir()
        output_dir.mkdir()

    monkeypatch.setattr(images_module, "materialize_runtime_assets", materialize)

    # When
    images_module._stage_build_context(Path.cwd(), tmp_path, "amd64")

    # Then
    assert (
        tmp_path / "templates" / "coder" / "runtime" / "Dockerfile"
    ).is_file()


def test_coder_runtime_builder_persists_inspected_ids_before_template_rendering(
    tmp_path: Path,
) -> None:
    # Given
    source = parse_env_file(Path("fixtures/full.env"))
    raw = RawEnvInput(format_version=1, values={**source.values, "PACKS": "coder"})
    desired = resolve_desired_state(raw)
    assert desired.shared_core.postgres is not None
    persist_install_scaffold(tmp_path, raw, desired)
    ensure_litellm_generated_keys(tmp_path)
    ensure_surfsense_generated_secrets(tmp_path)
    ensure_seaweedfs_generated_secrets(tmp_path)

    class FixtureAdapter:
        def ensure_image(
            self, *, manifest: RuntimeManifest, architecture: str
        ) -> DerivedImageInspection:
            return DerivedImageInspection(
                architecture=architecture,
                tag=manifest.derived_image_tag(architecture),
                image_id="sha256:" + ("a" if architecture == "amd64" else "b") * 64,
                labels={
                    "org.opencontainers.image.base.name": manifest.workspace_base_image,
                    "org.opencontainers.image.revision": manifest.lock_sha256,
                    "org.opencontainers.image.created": str(manifest.source_date_epoch),
                },
                layers=("sha256:" + "c" * 64,),
            )

    backend = coder_module.DokployCoderBackend(
        api_url="http://127.0.0.1:3000",
        api_key="test-key",
        stack_name=desired.stack_name,
        hostname=desired.hostnames["coder"],
        wildcard_hostname=desired.hostnames.get("coder-wildcard"),
        admin_email="admin@example.com",
        admin_password="test-password",
        postgres_service_name=desired.shared_core.postgres.service_name,
        postgres=SharedPostgresAllocation(
            database_name="coder",
            user_name="coder",
            password_secret_ref="coder-password",
        ),
        state_dir=tmp_path,
        runtime_images=desired.runtime_images,
        workspace_image_adapter=FixtureAdapter(),
    )

    # When
    replacements = backend._workspace_runtime_image_replacements()
    persisted = load_state_dir(tmp_path)

    # Then
    assert replacements == {
        "__DOKPLOY_WIZARD_RUNTIME_IMAGE_AMD64__": "sha256:" + "a" * 64,
        "__DOKPLOY_WIZARD_RUNTIME_IMAGE_ARM64__": "sha256:" + "b" * 64,
    }
    assert persisted.desired_state is not None
    assert persisted.applied_state is not None
    assert persisted.desired_state.runtime_images.workspace_amd64 == "sha256:" + "a" * 64
    assert persisted.applied_state.runtime_images is not None
    assert persisted.applied_state.runtime_images.workspace_arm64 == "sha256:" + "b" * 64
