from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_archives import (
    extract_node_archive,
    extract_zellij_binary_archive,
)


def test_node_archive_reconstructs_only_the_controlled_npm_links(tmp_path: Path) -> None:
    # Given
    archive_path = tmp_path / "node.tar.xz"
    root = "node-v22.19.0-linux-x64"
    with tarfile.open(archive_path, "w:xz") as archive:
        _add_directory(archive, f"{root}/")
        _add_directory(archive, f"{root}/bin/")
        _add_directory(archive, f"{root}/lib/node_modules/npm/bin/")
        _add_file(archive, f"{root}/bin/node", b"node", 0o755)
        _add_file(archive, f"{root}/lib/node_modules/npm/bin/npm-cli.js", b"npm", 0o644)
        _add_file(archive, f"{root}/lib/node_modules/npm/bin/npx-cli.js", b"npx", 0o644)
        _add_link(archive, f"{root}/bin/npm", "../lib/node_modules/npm/bin/npm-cli.js")
        _add_link(archive, f"{root}/bin/npx", "../lib/node_modules/npm/bin/npx-cli.js")

    # When
    destination = tmp_path / "node"
    extract_node_archive(archive_path=archive_path, destination=destination, expected_root=root)

    # Then
    assert (destination / "bin" / "node").read_bytes() == b"node"
    assert os.readlink(destination / "bin" / "npm") == "../lib/node_modules/npm/bin/npm-cli.js"
    assert os.readlink(destination / "bin" / "npx") == "../lib/node_modules/npm/bin/npx-cli.js"


def test_zellij_archive_extracts_a_root_binary_without_relaxing_rooted_archives(
    tmp_path: Path,
) -> None:
    # Given
    archive_path = tmp_path / "zellij.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "zellij", b"zellij", 0o755)

    # When
    destination = tmp_path / "zellij"
    extract_zellij_binary_archive(archive_path=archive_path, destination=destination)

    # Then
    binary = destination / "bin" / "zellij"
    assert binary.read_bytes() == b"zellij"
    assert binary.stat().st_mode & 0o111


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    archive.addfile(member)


def _add_file(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.size = len(content)
    archive.addfile(member, io.BytesIO(content))


def _add_link(archive: tarfile.TarFile, name: str, target: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = target
    archive.addfile(member)
