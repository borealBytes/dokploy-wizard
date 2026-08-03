"""Parse and validate the immutable Coder workspace runtime manifest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_hermes_contract import parse_hermes_contract
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_contract import parse_kdense_contract
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    ARCHITECTURE_LOCKS,
    ARCHITECTURES,
    RUNTIME_PACKAGES,
    JsonValue,
    RuntimeAssetError,
    RuntimeManifest,
    require_digest_image,
    require_mapping,
    require_sha256,
    require_text,
)


def runtime_manifest_path() -> Path:
    """Return the checked-in workspace runtime lock path."""

    return Path(__file__).resolve().parents[1] / "runtime-manifest.lock.json"


def load_runtime_manifest(path: Path) -> RuntimeManifest:
    """Load the closed-schema manifest that defines all runtime build inputs."""

    try:
        raw = path.read_bytes()
        payload: JsonValue = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeAssetError("runtime manifest is unreadable") from error
    document = require_mapping(payload, "runtime manifest")
    if set(document) != {"schema_version", "images", "workspace_runtime"}:
        raise RuntimeAssetError("runtime manifest keys are invalid")
    if document["schema_version"] != 3:
        raise RuntimeAssetError("runtime manifest version is invalid")
    images = require_mapping(document["images"], "runtime images")
    if set(images) != {"coder"}:
        raise RuntimeAssetError("runtime image keys are invalid")
    coder = require_digest_image(images["coder"], "coder image")
    runtime = require_mapping(document["workspace_runtime"], "workspace runtime")
    expected = {
        "base_image",
        "source_date_epoch",
        "providers",
        "opencode",
        "zellij",
        "node",
        "proxy_node",
        "pi",
        "ubuntu_snapshot",
        "kdense",
        "hermes",
    }
    if set(runtime) != expected:
        raise RuntimeAssetError("workspace runtime keys are invalid")
    base = require_digest_image(runtime["base_image"], "workspace base image")
    if runtime["source_date_epoch"] != 1782864000:
        raise RuntimeAssetError("workspace source date epoch is invalid")
    _providers(require_mapping(runtime["providers"], "runtime providers"))
    release_assets = _release_assets(require_mapping(runtime["opencode"], "OpenCode release"))
    _architecture_assets(require_mapping(runtime["zellij"], "Zellij"), "zellij")
    _architecture_assets(require_mapping(runtime["node"], "Node"), "node")
    _architecture_assets(require_mapping(runtime["proxy_node"], "proxy Node"), "proxy_node")
    _pi(require_mapping(runtime["pi"], "Pi"))
    _snapshot(require_mapping(runtime["ubuntu_snapshot"], "Ubuntu snapshot"))
    kdense = parse_kdense_contract(runtime["kdense"])
    parse_hermes_contract(runtime["hermes"])
    return RuntimeManifest(
        coder_image=coder,
        workspace_base_image=base,
        source_date_epoch=1782864000,
        release_assets=release_assets,
        lock_sha256=hashlib.sha256(raw).hexdigest(),
        kdense=kdense,
    )


def _providers(value: Mapping[str, JsonValue]) -> None:
    expected = {"coder", "docker", "code_server_module", "code_server_install", "platform_sha256"}
    locked = {
        "coder": "2.18.0",
        "docker": "4.5.0",
        "code_server_module": "1.5.2",
        "code_server_install": "4.117.0",
    }
    if set(value) != expected or {key: value[key] for key in locked} != locked:
        raise RuntimeAssetError("runtime provider lock is invalid")
    platforms = require_mapping(value["platform_sha256"], "runtime provider platform hashes")
    if set(platforms) != {"coder", "docker"}:
        raise RuntimeAssetError("runtime provider platform hashes are invalid")
    for provider_name in ("coder", "docker"):
        hashes = require_mapping(platforms[provider_name], "runtime provider platform hash")
        require_sha256(hashes["amd64"], "runtime provider amd64 hash")
        require_sha256(hashes["arm64"], "runtime provider arm64 hash")


def _release_assets(value: Mapping[str, JsonValue]) -> dict[str, str]:
    expected = {"release_id", "immutable", "x64", "arm64"}
    if set(value) != expected or value["release_id"] != 355168624 or value["immutable"] is not True:
        raise RuntimeAssetError("OpenCode release lock is invalid")
    result: dict[str, str] = {}
    for architecture, asset_id, checksum in (
        ("x64", 479302078, "60f27b2679f00a511b6539f97e02448afaf58d9c66e2448285ea0c517ca84583"),
        ("arm64", 479302069, "da0a631174eba380b2a1d51f9d364fa3812da433e72743c72471d4b5da59c69d"),
    ):
        asset = require_mapping(value[architecture], f"OpenCode {architecture} asset")
        if (
            set(asset) != {"asset_id", "url", "sha256"}
            or asset["asset_id"] != asset_id
            or not require_text(asset["url"], "OpenCode asset URL").startswith("https://api.github.com/")
            or asset["sha256"] != checksum
        ):
            raise RuntimeAssetError("OpenCode release sha256 is invalid")
        result[architecture] = checksum
    return result


def _architecture_assets(value: Mapping[str, JsonValue], name: str) -> None:
    version, assets = ARCHITECTURE_LOCKS[name]
    label = name.replace("_", " ")
    if set(value) != {"version", "amd64", "arm64"} or value["version"] != version:
        raise RuntimeAssetError(f"{label} lock is invalid")
    for architecture, (url, checksum) in assets.items():
        asset = require_mapping(value[architecture], f"{label} {architecture}")
        if set(asset) != {"url", "sha256"} or asset["url"] != url or asset["sha256"] != checksum:
            raise RuntimeAssetError(f"{label} lock is invalid")


def _pi(value: Mapping[str, JsonValue]) -> None:
    expected_keys = {
        "package", "version", "tarball_url", "tarball_sha512", "shrinkwrap_url",
        "shrinkwrap_sha256", "package_lock_url", "package_lock_sha256",
    }
    expected_values = {
        "package": "@earendil-works/pi-coding-agent",
        "version": "0.80.10",
        "tarball_url": "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-0.80.10.tgz",
        "tarball_sha512": (
            "aL4apbupCHiVLSXASXvRzH4Q2vmtfrDa+0s909CJuVu/GgGylbDzr7oyF1mPmip5E+VxYYxKWmph4hV04wUcQg=="
        ),
        "shrinkwrap_url": "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-0.80.10.tgz#package/npm-shrinkwrap.json",
        "shrinkwrap_sha256": "290c8147844e5b2129e8133d1c62f6d577e1fc5be7a563af04edb7eb17b3205f",
        "package_lock_url": "https://github.com/earendil-works/pi/releases/download/v0.80.10/pi-coding-agent-install-package-lock.json",
        "package_lock_sha256": "f0815f7807ae0aca1a53519fcdd37b20cc48d1ad6fa4161674e9a329e6ba004c",
    }
    if set(value) != expected_keys or dict(value) != expected_values:
        raise RuntimeAssetError("Pi package lock is invalid")


def _snapshot(value: Mapping[str, JsonValue]) -> None:
    if set(value) != {"timestamp", "suite", "inrelease", "package_indexes", "packages"}:
        raise RuntimeAssetError("Ubuntu snapshot lock is invalid")
    if value["timestamp"] != "20260701T000000Z" or value["suite"] != "noble":
        raise RuntimeAssetError("Ubuntu snapshot lock is invalid")
    inrelease = require_mapping(value["inrelease"], "Ubuntu InRelease")
    if set(inrelease) != {"url", "sha256"} or not require_text(
        inrelease["url"], "Ubuntu InRelease URL"
    ).startswith("https://snapshot.ubuntu.com/"):
        raise RuntimeAssetError("Ubuntu InRelease lock is invalid")
    require_sha256(inrelease["sha256"], "Ubuntu InRelease sha256")
    indexes = require_mapping(value["package_indexes"], "Ubuntu package indexes")
    packages = require_mapping(value["packages"], "Ubuntu packages")
    if frozenset(indexes) != ARCHITECTURES or frozenset(packages) != ARCHITECTURES:
        raise RuntimeAssetError("Ubuntu packages are incomplete")
    for architecture in ARCHITECTURES:
        architecture_indexes = require_mapping(
            indexes[architecture], f"Ubuntu {architecture} package indexes"
        )
        if set(architecture_indexes) != {"universe", "updates", "security"}:
            raise RuntimeAssetError("Ubuntu package indexes are incomplete")
        for checksum in architecture_indexes.values():
            require_sha256(checksum, f"Ubuntu {architecture} package index sha256")
        _package_records(packages[architecture], architecture)


def _package_records(value: JsonValue, architecture: str) -> None:
    if not isinstance(value, list) or not value:
        raise RuntimeAssetError("Ubuntu packages are incomplete")
    names: set[str] = set()
    for record in value:
        package = require_mapping(record, f"Ubuntu {architecture} package")
        if set(package) != {"name", "version", "arch", "url", "sha256"}:
            raise RuntimeAssetError("Ubuntu package lock is invalid")
        name = require_text(package["name"], "Ubuntu package name")
        if name in names or package["arch"] not in {architecture, "all"}:
            raise RuntimeAssetError("Ubuntu package lock is invalid")
        names.add(name)
        require_text(package["version"], "Ubuntu package version")
        url = require_text(package["url"], "Ubuntu package URL")
        if not url.startswith("https://snapshot.ubuntu.com/") or not url.endswith(".deb"):
            raise RuntimeAssetError("Ubuntu package lock is invalid")
        require_sha256(package["sha256"], "Ubuntu package sha256")
    if names != RUNTIME_PACKAGES:
        raise RuntimeAssetError("Ubuntu packages are incomplete")
