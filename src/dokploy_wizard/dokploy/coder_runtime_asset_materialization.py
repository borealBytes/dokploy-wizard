"""Download and assemble verified Coder runtime payloads."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.request import Request, urlopen

from dokploy_wizard.dokploy.coder_runtime_asset_archives import (
    extract_node_archive,
    extract_runtime_archive,
    extract_zellij_binary_archive,
)
from dokploy_wizard.dokploy.coder_runtime_asset_checksums import (
    write_materialized_checksums,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    JsonValue,
    RuntimeAssetError,
    require_mapping,
    require_sha256,
    require_text,
)

_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_MATERIALIZED_RUNTIME_PATHS: Final = frozenset(
    {
        "/opt/dokploy-wizard/runtime/install/opencode",
        "/opt/dokploy-wizard/runtime/install/node",
        "/opt/dokploy-wizard/runtime/install/proxy-node",
        "/opt/dokploy-wizard/runtime/install/zellij",
        "/opt/dokploy-wizard/runtime/install/pi",
        "/opt/dokploy-wizard/runtime/install/packages",
        "/opt/dokploy-wizard/runtime/install/hermes",
        "/opt/dokploy-wizard/runtime/install/kdense",
    }
)


def materialized_runtime_paths() -> frozenset[str]:
    """Return the absolute roots copied from each Task 6 materialization."""

    return _MATERIALIZED_RUNTIME_PATHS


@dataclass(frozen=True, slots=True)
class MaterializationContext:
    """One architecture-specific runtime payload assembly operation."""

    root: Path
    runtime: Mapping[str, JsonValue]
    architecture: str
    downloads: Path = field(init=False)
    install: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "downloads", self.root / "downloads")
        object.__setattr__(self, "install", self.root / "install")


@dataclass(frozen=True, slots=True)
class _DownloadSpec:
    url: str
    destination: Path
    sha256: str | None = None
    sha512: str | None = None


def materialize(context: MaterializationContext) -> None:
    """Download, validate, and assemble an install-ready payload in context.root."""

    context.downloads.mkdir(parents=True)
    opencode = require_mapping(context.runtime["opencode"], "OpenCode release")
    key = "x64" if context.architecture == "amd64" else "arm64"
    opencode_path = context.downloads / "opencode"
    download_runtime_asset(context, require_mapping(opencode[key], "OpenCode asset"), opencode_path)
    _copy_executable(opencode_path, context.install / "opencode" / "bin" / "opencode")
    node_path = _materialize_node(context)
    _materialize_archive(context, "proxy_node", context.install / "proxy-node")
    _materialize_zellij(context)
    _materialize_pi(context, node_path)
    _materialize_packages(context)
    from dokploy_wizard.dokploy.coder_runtime_asset_hermes_materialization import (
        materialize_hermes_assets,
    )

    materialize_hermes_assets(context)
    from dokploy_wizard.dokploy.coder_runtime_asset_kdense_materialization import (
        materialize_kdense_assets,
    )

    materialize_kdense_assets(context)
    write_materialized_checksums(context.root)


def _materialize_archive(context: MaterializationContext, name: str, destination: Path) -> Path:
    locked = require_mapping(context.runtime[name], name)
    asset = require_mapping(locked[context.architecture], f"{name} asset")
    archive = context.downloads / f"{name}.tar.xz"
    download_runtime_asset(context, asset, archive)
    version = require_text(locked["version"], f"{name} version")
    suffix = "x64" if context.architecture == "amd64" else "arm64"
    root = f"node-{version}-linux-{suffix}"
    extract_runtime_archive(archive_path=archive, destination=destination, expected_root=root)
    return destination / "bin" / "node"


def _materialize_node(context: MaterializationContext) -> Path:
    locked = require_mapping(context.runtime["node"], "node")
    asset = require_mapping(locked[context.architecture], "node asset")
    archive = context.downloads / "node.tar.xz"
    download_runtime_asset(context, asset, archive)
    version = require_text(locked["version"], "node version")
    suffix = "x64" if context.architecture == "amd64" else "arm64"
    destination = context.install / "node"
    extract_node_archive(
        archive_path=archive,
        destination=destination,
        expected_root=f"node-{version}-linux-{suffix}",
    )
    return destination / "bin" / "node"


def _materialize_zellij(context: MaterializationContext) -> None:
    locked = require_mapping(context.runtime["zellij"], "zellij")
    asset = require_mapping(locked[context.architecture], "zellij asset")
    archive = context.downloads / "zellij.tar.gz"
    download_runtime_asset(context, asset, archive)
    extract_zellij_binary_archive(archive_path=archive, destination=context.install / "zellij")


def _materialize_pi(context: MaterializationContext, node_path: Path) -> None:
    pi = require_mapping(context.runtime["pi"], "Pi")
    tarball = context.downloads / "pi.tgz"
    _download(
        context,
        _DownloadSpec(
            url=require_text(pi["tarball_url"], "Pi tarball URL"),
            destination=tarball,
            sha512=require_text(pi["tarball_sha512"], "Pi tarball sha512"),
        ),
    )
    destination = context.install / "pi"
    extract_runtime_archive(
        archive_path=tarball, destination=destination, expected_root="package"
    )
    shrinkwrap = destination / "npm-shrinkwrap.json"
    shrinkwrap_sha256 = require_sha256(
        pi["shrinkwrap_sha256"], "Pi shrinkwrap sha256"
    )
    actual_shrinkwrap_sha256 = ""
    if shrinkwrap.is_file():
        actual_shrinkwrap_sha256 = hashlib.sha256(shrinkwrap.read_bytes()).hexdigest()
    if actual_shrinkwrap_sha256 != shrinkwrap_sha256:
        raise RuntimeAssetError("Pi shrinkwrap is invalid")
    package_lock = context.downloads / "pi-package-lock.json"
    _download(
        context,
        _DownloadSpec(
            url=require_text(pi["package_lock_url"], "Pi package lock URL"),
            destination=package_lock,
            sha256=require_sha256(pi["package_lock_sha256"], "Pi package lock sha256"),
        ),
    )
    shutil.copyfile(package_lock, shrinkwrap)
    (destination / "package-lock.json").unlink(missing_ok=True)
    _install_pi_dependencies(destination, node_path)


def _install_pi_dependencies(destination: Path, node_path: Path) -> None:
    npm_cli = node_path.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    result = subprocess.run(
        [str(node_path), str(npm_cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SOURCE_DATE_EPOCH": "1782864000"},
    )
    if result.returncode != 0:
        raise RuntimeAssetError("Pi offline dependency materialization failed")


def _materialize_packages(context: MaterializationContext) -> None:
    snapshot = require_mapping(context.runtime["ubuntu_snapshot"], "Ubuntu snapshot")
    packages = require_mapping(snapshot["packages"], "Ubuntu packages")
    records = packages[context.architecture]
    if not isinstance(records, list):
        raise RuntimeAssetError("Ubuntu packages are invalid")
    destination = context.install / "packages"
    destination.mkdir(parents=True)
    for record in records:
        package = require_mapping(record, "Ubuntu package")
        name = require_text(package["name"], "Ubuntu package name")
        downloaded = context.downloads / "packages" / f"{name}.deb"
        download_runtime_asset(context, package, downloaded)
        shutil.copy2(downloaded, destination / f"{name}.deb")


def download_runtime_asset(
    context: MaterializationContext, asset: Mapping[str, JsonValue], destination: Path
) -> None:
    _download(
        context,
        _DownloadSpec(
            url=require_text(asset["url"], "runtime asset URL"),
            destination=destination,
            sha256=require_sha256(asset["sha256"], "runtime asset sha256"),
        ),
    )


def _download(context: MaterializationContext, spec: _DownloadSpec) -> None:
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    written = 0
    request = Request(spec.url, headers={"Accept": "application/octet-stream"})
    try:
        with urlopen(request, timeout=30) as response, spec.destination.open("xb") as output:  # noqa: S310
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                written += len(chunk)
                if written > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeAssetError("runtime asset exceeds size limit")
                sha256.update(chunk)
                sha512.update(chunk)
                output.write(chunk)
    except RuntimeAssetError:
        spec.destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        spec.destination.unlink(missing_ok=True)
        raise RuntimeAssetError("runtime asset download failed") from error
    actual_sha256 = sha256.hexdigest()
    actual_sha512 = base64.b64encode(sha512.digest()).decode("ascii")
    if (spec.sha256 is not None and actual_sha256 != spec.sha256) or (
        spec.sha512 is not None and actual_sha512 != spec.sha512
    ):
        spec.destination.unlink(missing_ok=True)
        raise RuntimeAssetError("runtime asset checksum is invalid")


def _copy_executable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755)
