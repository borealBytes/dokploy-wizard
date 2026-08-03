from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder import _rendered_template_dir, _template_version_name
from dokploy_wizard.dokploy.coder_runtime_assets import (
    DerivedImageInspection,
    RuntimeAssetError,
    load_runtime_manifest,
    runtime_manifest_path,
    validate_derived_image,
    validate_template_runtime_inputs,
)


def test_pinned_assets_and_derived_image_are_architecture_specific() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())

    # When
    amd64 = manifest.derived_image_tag("amd64")
    arm64 = manifest.derived_image_tag("arm64")

    # Then
    assert manifest.workspace_base_image.endswith(
        "4e01f4c2cc588f3ddd1d8f0591574141689fe6eac82b2040e1ffd587a74877f2"
    )
    assert amd64.startswith("dokploy-wizard/coder-runtime:")
    assert arm64.startswith("dokploy-wizard/coder-runtime:")
    assert amd64 != arm64
    assert manifest.coder_image.startswith("ghcr.io/coder/coder@sha256:")


def test_runtime_asset_error_preserves_python_traceback_state() -> None:
    # Given
    def raise_runtime_asset_error() -> None:
        raise RuntimeAssetError("invalid runtime asset")

    # When
    with pytest.raises(RuntimeAssetError) as captured:
        raise_runtime_asset_error()

    # Then
    assert captured.value.__traceback__ is not None


def test_manifest_rejects_empty_declared_architecture_package_records(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "runtime-manifest.lock.json"
    document = json.loads(runtime_manifest_path().read_text(encoding="utf-8"))
    document["workspace_runtime"]["ubuntu_snapshot"]["packages"]["arm64"] = []
    path.write_text(json.dumps(document), encoding="utf-8")

    # When / Then
    with pytest.raises(RuntimeAssetError, match="packages"):
        load_runtime_manifest(path)


def test_render_digest_consumes_runtime_lock(tmp_path: Path) -> None:
    # Given
    template = tmp_path / "template"
    template.mkdir()
    (template / "main.tf").write_text("terraform {}\n")

    # When
    rendered = _template_version_name(template_dir=template, replacements=None)

    # Then
    assert rendered == f"dokploy-wizard-{render_digest(template)[:16]}"


def test_terraform_locks_cover_both_workspace_architectures() -> None:
    # Given
    root = Path(__file__).resolve().parents[2] / "templates" / "coder"
    locks = tuple(root.glob("*/.terraform.lock.hcl"))

    # When
    contents = tuple(lock.read_text() for lock in locks)

    # Then
    assert len(locks) == 6
    assert all('version     = "2.18.0"' in content for content in contents)
    assert all('version     = "4.5.0"' in content for content in contents)
    assert all("--ignore-lockfile" not in content for content in contents)


def test_real_templates_accept_retained_downstream_application_bootstraps() -> None:
    # Given
    templates = tuple(Path("templates/coder").glob("*/main.tf"))

    # When / Then
    validate_template_runtime_inputs(templates)


def test_real_templates_select_persisted_workspace_image_ids() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())
    templates = tuple(Path("templates/coder").glob("*/main.tf"))

    # When
    contents = tuple(template.read_text(encoding="utf-8") for template in templates)

    # Then
    assert len(contents) == 6
    assert all(manifest.workspace_base_image not in content for content in contents)
    assert all('resource "docker_image" "workspace"' in content for content in contents)
    assert all("docker_image.workspace.image_id" in content for content in contents)


def test_workspace_base_image_is_rejected_as_a_derived_runtime_image(tmp_path: Path) -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())
    template = tmp_path / "main.tf"
    template.write_text(
        "resource \"docker_container\" \"workspace\" "
        f'{{ image = "{manifest.workspace_base_image}" }}\n',
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(RuntimeAssetError, match="derived"):
        validate_template_runtime_inputs((template,))


def test_runtime_dockerfile_binds_the_immutable_materialization_contract() -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "templates" / "coder" / "runtime" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    materializer = root / "templates" / "coder" / "runtime" / "materialize-runtime-assets"

    # When / Then
    assert materializer.is_file()
    assert "runtime-manifest.lock.json" in dockerfile
    assert "RUNTIME_MANIFEST_SHA256" in dockerfile
    assert "ENV SOURCE_DATE_EPOCH" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "verify-runtime-assets /opt/dokploy-wizard/runtime" in dockerfile
    assert "dpkg --install" in dockerfile
    assert "install -m 0755" in dockerfile


def test_template_validation_rejects_a_mutable_workspace_image(tmp_path: Path) -> None:
    # Given
    template = tmp_path / "main.tf"
    template.write_text(
        'resource "docker_image" "workspace" { name = local.runtime_image }\n'
        'resource "docker_container" "workspace" { image = "example:latest" }\n',
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(RuntimeAssetError, match="persisted"):
        validate_template_runtime_inputs((template,))


@pytest.mark.parametrize(
    "content",
    [
        "curl -fsSL https://example.invalid/install | bash\n",
        "terraform init --ignore-lockfile\n",
        "git clone --branch main https://example.invalid/source\n",
    ],
)
def test_downstream_application_bootstrap_is_not_a_task6_runtime_input(
    content: str, tmp_path: Path
) -> None:
    # Given
    template = tmp_path / "main.tf"
    template.write_text(
        'resource "docker_image" "workspace" { name = local.runtime_image }\n'
        'resource "docker_container" "workspace" { image = docker_image.workspace.image_id }\n'
        + content,
        encoding="utf-8",
    )

    # When
    validate_template_runtime_inputs((template,))


def test_checksum_mismatch_rejects_modified_manifest(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "runtime-manifest.lock.json"
    path.write_text(runtime_manifest_path().read_text().replace("60f27b", "x0f27b", 1))

    # When / Then
    with pytest.raises(RuntimeAssetError, match="sha256"):
        load_runtime_manifest(path)


def test_image_label_mismatch_rejects_derived_runtime_fixture() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())
    labels = {
        "org.opencontainers.image.base.name": manifest.workspace_base_image,
        "org.opencontainers.image.revision": "0" * 64,
        "org.opencontainers.image.created": str(manifest.source_date_epoch),
    }

    # When / Then
    with pytest.raises(RuntimeAssetError, match="label"):
        validate_derived_image(
            manifest,
            DerivedImageInspection(
                architecture="amd64",
                tag=manifest.derived_image_tag("amd64"),
                image_id="sha256:" + "a" * 64,
                labels=labels,
                layers=("sha256:" + "b" * 64,),
            ),
        )


def test_lock_drift_rejects_runtime_provider_fixture(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "runtime-manifest.lock.json"
    drifted = runtime_manifest_path().read_text().replace(
        '"coder": "2.18.0"', '"coder": "2.18.1"'
    )
    path.write_text(drifted)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="provider"):
        load_runtime_manifest(path)


def test_lock_drift_rejects_runtime_node_fixture(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "runtime-manifest.lock.json"
    drifted = runtime_manifest_path().read_text().replace(
        "c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2",
        "0" * 64,
    )
    path.write_text(drifted)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="node"):
        load_runtime_manifest(path)


def render_digest(template: Path) -> str:
    digest = hashlib.sha256()
    digest.update(template.name.encode("utf-8"))
    digest.update(b"\x00")
    with _rendered_template_dir(template_dir=template, replacements=None) as rendered:
        for path in sorted(path for path in rendered.rglob("*") if path.is_file()):
            digest.update(path.relative_to(rendered).as_posix().encode("utf-8"))
            digest.update(b"\x00")
            digest.update(path.read_bytes())
            digest.update(b"\x00")
    digest.update(b"runtime-manifest\x00")
    digest.update(load_runtime_manifest(runtime_manifest_path()).lock_sha256.encode("utf-8"))
    digest.update(b"\x00")
    return digest.hexdigest()
