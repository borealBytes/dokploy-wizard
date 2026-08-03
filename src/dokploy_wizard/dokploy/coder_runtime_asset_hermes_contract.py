"""Closed immutable source and toolchain contract for the Hermes workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    JsonValue,
    RuntimeAssetError,
    require_mapping,
    require_sha256,
    require_text,
)

HERMES_COMMIT: Final = "a7d7c02cb6db071eced4ac82e24f878588619600"
HERMES_ARCHIVE_SHA256: Final = "2a9cd3f205b9e0df5a687fe4cd61ac082a14d40ddc55149ee35b2b5093ab01d3"
CLASSIC_COMMIT: Final = "4d9965b37a5ca3dbec1c19ccdaa261211b180804"
CLASSIC_TREE: Final = "a9436fa15d5752bfbf20e281fe54f39869ea5388"
CLASSIC_ARCHIVE_SHA256: Final = "cd5f5d40ca5ad77336eb8048b226b05279721eace5cf14f8ca1cdb0513cb7662"
CPYTHON_VERSION: Final = "3.11.15+20260303"
UV_VERSION: Final = "0.11.29"
PYYAML_VERSION: Final = "6.0.3"


@dataclass(frozen=True, slots=True)
class HermesChecksumAsset:
    """One immutable Hermes download."""

    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class HermesArchitectureAssets:
    """Architecture-specific Hermes tool downloads."""

    version: str
    amd64: HermesChecksumAsset
    arm64: HermesChecksumAsset


@dataclass(frozen=True, slots=True)
class HermesIconAsset:
    """One icon copied from an already verified pinned source archive."""

    archive: Literal["source", "classic"]
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class HermesIconAssets:
    """The three Hermes icons retained from the pinned archives."""

    dashboard: HermesIconAsset
    separate_webui: HermesIconAsset
    classic: HermesIconAsset


@dataclass(frozen=True, slots=True)
class HermesRuntimeContract:
    """Verified archives and lock hashes consumed by the Hermes workspace."""

    source: HermesChecksumAsset
    classic: HermesChecksumAsset
    icons: HermesIconAssets
    cpython: HermesArchitectureAssets
    uv: HermesArchitectureAssets
    pyyaml_amd64: str
    pyyaml_arm64: str


def parse_hermes_contract(value: JsonValue) -> HermesRuntimeContract:
    """Parse the exact Task 14 Hermes runtime contract without fallback sources."""

    document = require_mapping(value, "Hermes runtime contract")
    if set(document) != {"source", "classic", "icons", "tools", "python_packages"}:
        raise RuntimeAssetError("Hermes runtime contract keys are invalid")
    source = _source(require_mapping(document["source"], "Hermes source"))
    classic = _classic(require_mapping(document["classic"], "Hermes Classic source"))
    tools = require_mapping(document["tools"], "Hermes tools")
    if set(tools) != {"cpython", "uv"}:
        raise RuntimeAssetError("Hermes tool keys are invalid")
    packages = require_mapping(document["python_packages"], "Hermes Python packages")
    if set(packages) != {"pyyaml"}:
        raise RuntimeAssetError("Hermes Python package keys are invalid")
    return HermesRuntimeContract(
        source=source,
        classic=classic,
        icons=_icons(require_mapping(document["icons"], "Hermes icons")),
        cpython=_architecture_assets(
            tools["cpython"], CPYTHON_VERSION, "Hermes CPython"
        ),
        uv=_architecture_assets(tools["uv"], UV_VERSION, "Hermes uv"),
        pyyaml_amd64=_pyyaml_hash(packages["pyyaml"], "amd64"),
        pyyaml_arm64=_pyyaml_hash(packages["pyyaml"], "arm64"),
    )


def _icons(value: Mapping[str, JsonValue]) -> HermesIconAssets:
    expected: dict[str, HermesIconAsset] = {
        "dashboard": HermesIconAsset(
            archive="source",
            path="acp_registry/icon.svg",
            sha256="8f6157ebb2ca034bec36094205e47777c794f9586fe33b6ca7e11e5a0a0dd6a3",
        ),
        "separate_webui": HermesIconAsset(
            archive="source",
            path="website/static/img/favicon.svg",
            sha256="c4d55805bda8e16072ed77c0725176ab2218a9e628d1ba776a6048a39c28f751",
        ),
        "classic": HermesIconAsset(
            archive="classic",
            path="static/favicon.svg",
            sha256="b883cc5f4e2fa5ee01cc87e1ed1546ba9ab47079632ce692a77cfa65178bb6d6",
        ),
    }
    if set(value) != set(expected):
        raise RuntimeAssetError("Hermes icon lock is invalid")
    parsed: dict[str, HermesIconAsset] = {}
    for name, expected_icon in expected.items():
        icon = require_mapping(value[name], f"Hermes {name} icon")
        if dict(icon) != {
            "archive": expected_icon.archive,
            "path": expected_icon.path,
            "sha256": expected_icon.sha256,
        }:
            raise RuntimeAssetError("Hermes icon lock is invalid")
        parsed[name] = expected_icon
    return HermesIconAssets(
        dashboard=parsed["dashboard"],
        separate_webui=parsed["separate_webui"],
        classic=parsed["classic"],
    )


def _source(value: Mapping[str, JsonValue]) -> HermesChecksumAsset:
    expected = {
        "repository": "NousResearch/hermes-agent",
        "commit": HERMES_COMMIT,
        "archive_url": f"https://codeload.github.com/NousResearch/hermes-agent/tar.gz/{HERMES_COMMIT}",
        "archive_sha256": HERMES_ARCHIVE_SHA256,
    }
    if dict(value) != expected:
        raise RuntimeAssetError("Hermes source pin is invalid")
    return HermesChecksumAsset(url=expected["archive_url"], sha256=HERMES_ARCHIVE_SHA256)


def _classic(value: Mapping[str, JsonValue]) -> HermesChecksumAsset:
    expected = {
        "repository": "nesquena/hermes-webui",
        "commit": CLASSIC_COMMIT,
        "tree": CLASSIC_TREE,
        "archive_url": f"https://codeload.github.com/nesquena/hermes-webui/tar.gz/{CLASSIC_COMMIT}",
        "archive_sha256": CLASSIC_ARCHIVE_SHA256,
    }
    if dict(value) != expected:
        raise RuntimeAssetError("Hermes Classic source pin is invalid")
    return HermesChecksumAsset(url=expected["archive_url"], sha256=CLASSIC_ARCHIVE_SHA256)


def _architecture_assets(
    value: JsonValue, version: str, label: str
) -> HermesArchitectureAssets:
    document = require_mapping(value, label)
    if set(document) != {"version", "amd64", "arm64"} or document["version"] != version:
        raise RuntimeAssetError(f"{label} lock is invalid")
    return HermesArchitectureAssets(
        version=version,
        amd64=_asset(document["amd64"], label),
        arm64=_asset(document["arm64"], label),
    )


def _asset(value: JsonValue, label: str) -> HermesChecksumAsset:
    document = require_mapping(value, f"{label} asset")
    if set(document) != {"url", "sha256"}:
        raise RuntimeAssetError(f"{label} lock is invalid")
    return HermesChecksumAsset(
        url=require_text(document["url"], f"{label} URL"),
        sha256=require_sha256(document["sha256"], f"{label} hash"),
    )


def _pyyaml_hash(value: JsonValue, architecture: str) -> str:
    document = require_mapping(value, "Hermes PyYAML")
    if set(document) != {"version", "cp311"} or document["version"] != PYYAML_VERSION:
        raise RuntimeAssetError("Hermes PyYAML lock is invalid")
    cp311 = require_mapping(document["cp311"], "Hermes PyYAML cp311")
    expected = {
        "amd64": "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d",
        "arm64": "10892704fc220243f5305762e276552a0395f7beb4dbf9b14ec8fd43b57f126c",
    }
    if dict(cp311) != expected:
        raise RuntimeAssetError("Hermes PyYAML lock is invalid")
    return expected[architecture]
