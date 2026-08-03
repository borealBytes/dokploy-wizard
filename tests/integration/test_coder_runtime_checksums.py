from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_runtime_asset_checksums import (
    verify_materialized_tree,
    write_materialized_checksums,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError


def test_checksum_manifest_covers_installed_files_and_controlled_links(tmp_path: Path) -> None:
    # Given
    install = tmp_path / "install" / "node" / "bin"
    install.mkdir(parents=True)
    executable = install / "node"
    executable.write_bytes(b"node")
    (install / "npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")

    # When
    write_materialized_checksums(tmp_path)

    # Then
    verify_materialized_tree(tmp_path)
    assert "install/node/bin/node" in (tmp_path / "checksums.sha256").read_text()
    assert "install/node/bin/npm" in (tmp_path / "links.sha256").read_text()


def test_checksum_verification_rejects_tampered_installed_file(tmp_path: Path) -> None:
    # Given
    binary = tmp_path / "install" / "zellij" / "bin" / "zellij"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"zellij")
    write_materialized_checksums(tmp_path)
    binary.write_bytes(b"tampered")

    # When / Then
    with pytest.raises(RuntimeAssetError, match="checksum"):
        verify_materialized_tree(tmp_path)


def test_offline_build_verifier_rejects_tampered_installed_file(tmp_path: Path) -> None:
    # Given
    binary = tmp_path / "install" / "opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"opencode")
    write_materialized_checksums(tmp_path)
    verifier = Path("templates/coder/runtime/verify-runtime-assets")
    binary.write_bytes(b"tampered")

    # When
    result = subprocess.run(["sh", str(verifier), str(tmp_path)], check=False, capture_output=True)

    # Then
    assert result.returncode != 0
