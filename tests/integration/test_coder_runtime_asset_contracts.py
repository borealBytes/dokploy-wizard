from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from dokploy_wizard.dokploy import coder_runtime_assets
from dokploy_wizard.dokploy.coder_runtime_assets import (
    DerivedImageInspection,
    RuntimeAssetError,
    load_runtime_manifest,
    runtime_manifest_path,
    validate_derived_image,
    validate_template_runtime_inputs,
)


def test_validated_derived_image_returns_the_inspected_full_id() -> None:
    # Given
    manifest = load_runtime_manifest(runtime_manifest_path())
    inspection = DerivedImageInspection(
        architecture="amd64",
        tag=manifest.derived_image_tag("amd64"),
        image_id="sha256:" + "a" * 64,
        labels={
            "org.opencontainers.image.base.name": manifest.workspace_base_image,
            "org.opencontainers.image.revision": manifest.lock_sha256,
            "org.opencontainers.image.created": str(manifest.source_date_epoch),
        },
        layers=("sha256:" + "b" * 64,),
    )

    # When
    image_id = validate_derived_image(manifest, inspection)

    # Then
    assert image_id == inspection.image_id


def test_template_validation_requires_persisted_workspace_image_ids(tmp_path: Path) -> None:
    # Given
    template = tmp_path / "main.tf"
    template.write_text(
        'resource "docker_image" "workspace" { name = local.runtime_image }\n'
        'resource "docker_container" "workspace" { image = docker_image.workspace.image_id }\n',
        encoding="utf-8",
    )

    # When / Then
    validate_template_runtime_inputs((template,))


def test_runtime_archive_rejects_a_traversal_member(tmp_path: Path) -> None:
    # Given
    archive_path = tmp_path / "node.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        member = tarfile.TarInfo("node-v22.19.0-linux-x64/../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    # When / Then
    with pytest.raises(RuntimeAssetError, match="archive"):
        coder_runtime_assets.extract_runtime_archive(
            archive_path=archive_path,
            destination=tmp_path / "payload",
            expected_root="node-v22.19.0-linux-x64",
        )


@pytest.mark.parametrize(
    ("members", "expected_root"),
    [
        (("foreign-root/bin/node",), "node-v22.19.0-linux-x64"),
        (("node-v22.19.0-linux-x64/bin/node",) * 2, "node-v22.19.0-linux-x64"),
    ],
)
def test_runtime_archive_rejects_unexpected_or_duplicate_destinations(
    tmp_path: Path, members: tuple[str, ...], expected_root: str
) -> None:
    # Given
    archive_path = tmp_path / "node.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        for name in members:
            member = tarfile.TarInfo(name)
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))

    # When / Then
    with pytest.raises(RuntimeAssetError, match="archive"):
        coder_runtime_assets.extract_runtime_archive(
            archive_path=archive_path,
            destination=tmp_path / "payload",
            expected_root=expected_root,
        )


def test_runtime_archive_rejects_symbolic_links_and_special_files(tmp_path: Path) -> None:
    # Given
    archive_path = tmp_path / "node.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        link = tarfile.TarInfo("node-v22.19.0-linux-x64/bin/node")
        link.type = tarfile.SYMTYPE
        link.linkname = "/bin/sh"
        archive.addfile(link)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="archive"):
        coder_runtime_assets.extract_runtime_archive(
            archive_path=archive_path,
            destination=tmp_path / "payload",
            expected_root="node-v22.19.0-linux-x64",
        )


def test_runtime_archive_accepts_an_explicit_expected_root_directory(tmp_path: Path) -> None:
    # Given
    archive_path = tmp_path / "node.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        root = tarfile.TarInfo("node-v22.19.0-linux-x64/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        member = tarfile.TarInfo("node-v22.19.0-linux-x64/bin/node")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    # When
    coder_runtime_assets.extract_runtime_archive(
        archive_path=archive_path,
        destination=tmp_path / "payload",
        expected_root="node-v22.19.0-linux-x64",
    )

    # Then
    assert (tmp_path / "payload" / "bin" / "node").read_bytes() == b"x"


def test_materializer_declares_checked_offline_install_payloads() -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    materializer_path = root / "templates" / "coder" / "runtime" / "materialize-runtime-assets"
    materializer = materializer_path.read_text(encoding="utf-8")
    implementation = (
        root / "src" / "dokploy_wizard" / "dokploy" / "coder_runtime_asset_materialization.py"
    ).read_text(encoding="utf-8")

    # When / Then
    assert "coder_runtime_asset_materializer" in materializer
    assert "extract_runtime_archive" in implementation
    assert 'install / "opencode" / "bin" / "opencode"' in implementation
    assert "extract_zellij_binary_archive" in implementation
    assert 'install / "node"' in implementation
    assert 'install / "proxy-node"' in implementation
    assert 'install / "pi"' in implementation


def test_hermes_template_uses_the_environment_virtual_key_reference() -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    template = (
        root / "templates" / "coder" / "default-ubuntu-code-server-hermes" / "main.tf"
    ).read_text(encoding="utf-8")

    # When / Then
    assert template.count("$${LITELLM_VIRTUAL_KEY_CODER_HERMES}") == 2
    assert template.count('"key_env": "OPENAI_API_KEY"') == 2
    assert '"api_key": api_key' not in template
