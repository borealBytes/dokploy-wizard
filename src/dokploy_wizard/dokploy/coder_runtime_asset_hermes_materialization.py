"""Materialize the immutable Hermes workspace payload from Task 14 pins."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from dokploy_wizard.dokploy.coder_runtime_asset_archives import extract_runtime_archive
from dokploy_wizard.dokploy.coder_runtime_asset_hermes_contract import (
    CLASSIC_COMMIT,
    HERMES_COMMIT,
    HermesChecksumAsset,
    HermesIconAsset,
    parse_hermes_contract,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.coder_runtime_asset_materialization import (
        MaterializationContext,
    )


@dataclass(frozen=True, slots=True)
class HermesArchive:
    """One verified archive and the fixed location where it is extracted."""

    asset: HermesChecksumAsset
    archive: Path
    destination: Path
    expected_root: str


def materialize_hermes_assets(context: MaterializationContext) -> None:
    """Download, verify, and extract pinned Hermes sources and toolchains."""

    contract = parse_hermes_contract(context.runtime["hermes"])
    destination = context.install / "hermes"
    source = destination / "source"
    classic = destination / "classic"
    tools = destination / "tools"
    _download_and_extract(
        context,
        HermesArchive(
            contract.source,
            context.downloads / "hermes-agent.tar.gz",
            source,
            f"hermes-agent-{HERMES_COMMIT}",
        ),
    )
    _require_lock(source, contract.pyyaml_amd64, contract.pyyaml_arm64)
    _download_and_extract(
        context,
        HermesArchive(
            contract.classic,
            context.downloads / "hermes-classic.tar.gz",
            classic,
            f"hermes-webui-{CLASSIC_COMMIT}",
        ),
    )
    icons = destination / "icons"
    icons.mkdir(parents=True)
    for name, icon in (
        ("hermes-dashboard.svg", contract.icons.dashboard),
        ("hermes-webui.svg", contract.icons.separate_webui),
        ("hermes-classic.svg", contract.icons.classic),
    ):
        match icon.archive:
            case "source":
                root = source
            case "classic":
                root = classic
            case unreachable:
                assert_never(unreachable)
        _copy_icon(root, icons / name, icon)
    architecture = (
        contract.cpython.amd64
        if context.architecture == "amd64"
        else contract.cpython.arm64
    )
    _download_and_extract(
        context,
        HermesArchive(
            architecture,
            context.downloads / "hermes-python.tar.gz",
            tools / "python",
            "python",
        ),
    )
    uv = contract.uv.amd64 if context.architecture == "amd64" else contract.uv.arm64
    uv_root = (
        "uv-x86_64-unknown-linux-gnu"
        if context.architecture == "amd64"
        else "uv-aarch64-unknown-linux-gnu"
    )
    _download_and_extract(
        context,
        HermesArchive(
            uv,
            context.downloads / "hermes-uv.tar.gz",
            tools / "uv",
            uv_root,
        ),
    )


def _download_and_extract(
    context: MaterializationContext, archive: HermesArchive
) -> None:
    from dokploy_wizard.dokploy.coder_runtime_asset_materialization import (
        download_runtime_asset,
    )

    download_runtime_asset(
        context,
        {"url": archive.asset.url, "sha256": archive.asset.sha256},
        archive.archive,
    )
    extract_runtime_archive(
        archive_path=archive.archive,
        destination=archive.destination,
        expected_root=archive.expected_root,
    )


def _require_lock(source: Path, amd64_hash: str, arm64_hash: str) -> None:
    lock = source / "uv.lock"
    if not lock.is_file() or lock.is_symlink():
        raise RuntimeAssetError("Hermes upstream uv lock is unavailable")
    content = lock.read_text(encoding="utf-8")
    if amd64_hash not in content or arm64_hash not in content:
        raise RuntimeAssetError("Hermes PyYAML lock is invalid")


def _copy_icon(root: Path, destination: Path, icon: HermesIconAsset) -> None:
    source = root / icon.path
    if not source.is_file() or source.is_symlink():
        raise RuntimeAssetError("Hermes pinned icon is unavailable")
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != icon.sha256:
        raise RuntimeAssetError("Hermes pinned icon is invalid")
    destination.write_bytes(content)
