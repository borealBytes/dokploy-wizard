from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from dokploy_wizard.dokploy.coder_runtime_asset_archives import (
    extract_node_archive,
    extract_runtime_archive,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_contract import (
    parse_kdense_contract,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_patches import (
    apply_kdense_patches,
    verify_kdense_patch_files,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_source import (
    verify_kdense_source_archive,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.coder_runtime_asset_materialization import (
        MaterializationContext,
    )


def materialize_kdense_assets(context: MaterializationContext) -> None:
    contract = parse_kdense_contract(context.runtime["kdense"])
    destination = context.install / "kdense"
    source_archive = context.downloads / "kdense-source.tar.gz"
    _download(context, contract.codeload_url, contract.archive_sha256, source_archive)
    verify_kdense_source_archive(source_archive, contract)
    source = destination / "source"
    extract_runtime_archive(
        archive_path=source_archive,
        destination=source,
        expected_root=contract.archive_root,
    )
    repository_root = Path(__file__).resolve().parents[3]
    apply_kdense_patches(source, verify_kdense_patch_files(repository_root, contract))
    _materialize_node(context, destination, contract)
    _materialize_cpython(context, destination, contract)
    _materialize_uv(context, destination, contract)
    _materialize_skills(context, destination, contract)
    _build_web(context, source, destination / "tools" / "node" / "bin" / "node")


def _materialize_node(
    context: MaterializationContext, destination: Path, contract: KdenseRuntimeContract
) -> None:
    node = contract.node.amd64 if context.architecture == "amd64" else contract.node.arm64
    archive = context.downloads / "kdense-node.tar.xz"
    _download(context, node.url, node.sha256, archive)
    suffix = "x64" if context.architecture == "amd64" else "arm64"
    extract_node_archive(
        archive_path=archive,
        destination=destination / "tools" / "node",
        expected_root=f"node-{contract.node.version}-linux-{suffix}",
    )


def _materialize_cpython(
    context: MaterializationContext, destination: Path, contract: KdenseRuntimeContract
) -> None:
    cpython = contract.cpython.amd64 if context.architecture == "amd64" else contract.cpython.arm64
    archive = context.downloads / "kdense-cpython.tar.gz"
    _download(context, cpython.url, cpython.sha256, archive)
    extract_runtime_archive(
        archive_path=archive,
        destination=destination / "tools" / "python",
        expected_root="python",
    )


def _materialize_uv(
    context: MaterializationContext, destination: Path, contract: KdenseRuntimeContract
) -> None:
    uv = contract.uv.amd64 if context.architecture == "amd64" else contract.uv.arm64
    archive = context.downloads / "kdense-uv.tar.gz"
    _download(context, uv.url, uv.sha256, archive)
    root = (
        "uv-x86_64-unknown-linux-gnu"
        if context.architecture == "amd64"
        else "uv-aarch64-unknown-linux-gnu"
    )
    extract_runtime_archive(
        archive_path=archive,
        destination=destination / "tools" / "uv",
        expected_root=root,
    )


def _materialize_skills(
    context: MaterializationContext, destination: Path, contract: KdenseRuntimeContract
) -> None:
    archive = context.downloads / "kdense-skills.tar.gz"
    _download(context, contract.skills.archive_url, contract.skills.archive_sha256, archive)
    source = destination / "skills-source"
    extract_runtime_archive(
        archive_path=archive,
        destination=source,
        expected_root=f"scientific-agent-skills-{contract.skills.commit}",
    )
    skills = source / "skills"
    if (
        not skills.is_dir()
        or skills.is_symlink()
        or _canonical_sha256(skills) != contract.skills.canonical_sha256
    ):
        raise RuntimeAssetError("K-Dense skills canonical digest is invalid")


def _build_web(context: MaterializationContext, source: Path, node: Path) -> None:
    npm = node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
    for path, command in (
        (source / "server", ("ci", "--no-audit", "--no-fund")),
        (source / "web", ("ci", "--no-audit", "--no-fund")),
        (source / "web", ("run", "build")),
    ):
        result = subprocess.run(
            (str(node), str(npm), *command),
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "NEXT_PUBLIC_ADK_API_URL": "", "SOURCE_DATE_EPOCH": "1782864000"},
        )
        if result.returncode != 0:
            raise RuntimeAssetError("K-Dense locked build failed")


def _download(
    context: MaterializationContext, url: str, checksum: str, destination: Path
) -> None:
    from dokploy_wizard.dokploy.coder_runtime_asset_materialization import (
        download_runtime_asset,
    )

    download_runtime_asset(context, {"url": url, "sha256": checksum}, destination)


def _canonical_sha256(root: Path) -> str:
    records = bytearray()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root.parent).as_posix()):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(root.parent).as_posix().encode("ascii")
            records.extend(hashlib.sha256(path.read_bytes()).hexdigest().encode())
            records.extend(b"  " + relative + b"\n")
    if not records:
        raise RuntimeAssetError("K-Dense skills layout is invalid")
    return hashlib.sha256(records).hexdigest()
