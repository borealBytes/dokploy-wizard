"""Parse the closed Task 7 K-Dense source/build contract."""

from __future__ import annotations

from collections.abc import Mapping

from dokploy_wizard.dokploy.coder_runtime_asset_kdense_pins import (
    COST_FIELDS,
    CPYTHON,
    KDENSE_ARCHIVE,
    KDENSE_COMMIT,
    KDENSE_TREE,
    LIMITS,
    NODE,
    PROVENANCE,
    SOURCE_PATHS,
    UV,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import (
    KdenseArchitectureAssets,
    KdenseCatalogContract,
    KdenseChecksumAsset,
    KdenseFieldMapping,
    KdensePatch,
    KdenseRuntimeContract,
    KdenseSkillSource,
    KdenseSourcePath,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    JsonValue,
    RuntimeAssetError,
    require_mapping,
    require_sha256,
    require_text,
)


def parse_kdense_contract(value: JsonValue) -> KdenseRuntimeContract:
    """Parse the immutable K-Dense source/build attestation without fallback paths."""

    document = require_mapping(value, "K-Dense runtime contract")
    if set(document) != {"source", "tools", "skills", "patches", "catalog"}:
        raise RuntimeAssetError("K-Dense runtime contract keys are invalid")
    source = _source(require_mapping(document["source"], "K-Dense source"))
    tools = require_mapping(document["tools"], "K-Dense tools")
    if set(tools) != {"node", "cpython", "uv"}:
        raise RuntimeAssetError("K-Dense tool keys are invalid")
    skills = _skills(require_mapping(document["skills"], "K-Dense skills"))
    patches = _patches(document["patches"])
    catalog = _catalog(require_mapping(document["catalog"], "K-Dense catalog"))
    return KdenseRuntimeContract(
        repository="K-Dense-AI/k-dense-byok",
        commit=KDENSE_COMMIT,
        tree=KDENSE_TREE,
        codeload_url=(
            "https://codeload.github.com/K-Dense-AI/k-dense-byok/tar.gz/"
            f"{KDENSE_COMMIT}"
        ),
        archive_sha256=KDENSE_ARCHIVE,
        archive_root=f"k-dense-byok-{KDENSE_COMMIT}",
        source_paths=source,
        absent_paths=("pyproject.toml", "prep_sandbox.py", "uv.lock"),
        node=_architecture_assets(tools["node"], NODE, "K-Dense Node"),
        cpython=_architecture_assets(tools["cpython"], CPYTHON, "K-Dense CPython"),
        uv=_architecture_assets(tools["uv"], UV, "K-Dense uv"),
        skills=skills,
        patches=patches,
        catalog=catalog,
    )


def _source(value: Mapping[str, JsonValue]) -> tuple[KdenseSourcePath, ...]:
    expected = {
        "repository",
        "commit",
        "tree",
        "codeload_url",
        "archive_sha256",
        "archive_root",
        "required_paths",
        "root_pyproject_absent",
        "uv_lock_paths",
        "prep_sandbox_absent",
        "typescript_prep_path",
    }
    if set(value) != expected:
        raise RuntimeAssetError("K-Dense source keys are invalid")
    exact: dict[str, JsonValue] = {
        "repository": "K-Dense-AI/k-dense-byok",
        "commit": KDENSE_COMMIT,
        "tree": KDENSE_TREE,
        "codeload_url": (
            "https://codeload.github.com/K-Dense-AI/k-dense-byok/tar.gz/"
            f"{KDENSE_COMMIT}"
        ),
        "archive_sha256": KDENSE_ARCHIVE,
        "archive_root": f"k-dense-byok-{KDENSE_COMMIT}",
        "root_pyproject_absent": True,
        "uv_lock_paths": [],
        "prep_sandbox_absent": True,
        "typescript_prep_path": "server/src/prep.ts",
    }
    if any(value[key] != expected for key, expected in exact.items()):
        raise RuntimeAssetError("K-Dense source pin is invalid")
    paths = _source_paths(value["required_paths"], "K-Dense source path")
    if len(paths) != len(SOURCE_PATHS):
        raise RuntimeAssetError("K-Dense source paths are incomplete")
    for path in paths:
        if path.path not in SOURCE_PATHS:
            raise RuntimeAssetError("K-Dense source paths are incomplete")
    return paths


def _architecture_assets(
    value: JsonValue, expected: tuple[str, str, str, str, str], label: str
) -> KdenseArchitectureAssets:
    document = require_mapping(value, label)
    if set(document) != {"version", "amd64", "arm64"} or document["version"] != expected[0]:
        raise RuntimeAssetError(f"{label} lock is invalid")
    amd64 = _checksum_asset(document["amd64"], expected[1], expected[2], label)
    arm64 = _checksum_asset(document["arm64"], expected[3], expected[4], label)
    return KdenseArchitectureAssets(version=expected[0], amd64=amd64, arm64=arm64)


def _checksum_asset(value: JsonValue, url: str, digest: str, label: str) -> KdenseChecksumAsset:
    asset = require_mapping(value, f"{label} asset")
    if set(asset) != {"url", "sha256"} or asset["url"] != url or asset["sha256"] != digest:
        raise RuntimeAssetError(f"{label} lock is invalid")
    return KdenseChecksumAsset(url=url, sha256=digest)


def _skills(value: Mapping[str, JsonValue]) -> KdenseSkillSource:
    expected = {
        "repository": "K-Dense-AI/scientific-agent-skills",
        "commit": "3f825caafe149b7853ec8c4d1dd7f4553ea6b2a5",
        "tree": "0118c6a9ab9c9b470addf3edc86cc1261622654e",
        "subtree": "f6a5238100ac4c6a7a00cf623ddc3b738a6e1405",
        "archive_url": "https://codeload.github.com/K-Dense-AI/scientific-agent-skills/tar.gz/3f825caafe149b7853ec8c4d1dd7f4553ea6b2a5",
        "archive_sha256": "44a0aeb6246edc5944b5cc0f4c228e7c8dd003cb1e066f26534cf632461e11d1",
        "canonical_sha256": "e5982515e05204240a3087644ba62dbf16d29d674957867e7951572ac22c6a53",
    }
    if dict(value) != expected:
        raise RuntimeAssetError("K-Dense skills pin is invalid")
    return KdenseSkillSource(**expected)


def _patches(value: JsonValue) -> tuple[KdensePatch, ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise RuntimeAssetError("K-Dense patch order is invalid")
    expected = (
        (
            "pricing-cache",
            "templates/coder/runtime/patches/kdense-pricing-cache.patch",
            "76f4b8d12f4843ca6d612f34e4b2e089886d59f801f6eafa8138711e5180aad0",
        ),
        (
            "central-only",
            "templates/coder/runtime/patches/kdense-central-only.patch",
            "27f47437b191f26134452baa83604bad72fded51bc8fbcf6817463d109609ce5",
        ),
    )
    records = tuple(_patch(item, index) for index, item in enumerate(value))
    if tuple((patch.name, patch.path, patch.sha256) for patch in records) != expected:
        raise RuntimeAssetError("K-Dense patch order is invalid")
    return records


def _patch(value: JsonValue, index: int) -> KdensePatch:
    document = require_mapping(value, "K-Dense patch")
    if set(document) != {"name", "path", "sha256"}:
        raise RuntimeAssetError("K-Dense patch is invalid")
    return KdensePatch(
        name=require_text(document["name"], "K-Dense patch name"),
        path=require_text(document["path"], "K-Dense patch path"),
        sha256=require_sha256(document["sha256"], "K-Dense patch sha256"),
    )


def _catalog(value: Mapping[str, JsonValue]) -> KdenseCatalogContract:
    expected = {
        "cost_fields",
        "limits",
        "provenance",
        "central_only_environment",
        "rejected_paths",
    }
    if set(value) != expected:
        raise RuntimeAssetError("K-Dense catalog keys are invalid")
    costs = _field_mappings(value["cost_fields"], COST_FIELDS, "K-Dense cost mapping")
    limits = _field_mappings(value["limits"], LIMITS, "K-Dense limit mapping")
    provenance = _field_mappings(value["provenance"], PROVENANCE, "K-Dense provenance mapping")
    environment = _texts(value["central_only_environment"], "K-Dense central-only environment")
    rejected = _texts(value["rejected_paths"], "K-Dense rejected path")
    expected_environment = (
        "KDENSE_WIZARD_CENTRAL_ONLY=1",
        "KDENSE_LITELLM_BASE_URL",
        "KDENSE_LITELLM_API_KEY",
    )
    if environment != expected_environment:
        raise RuntimeAssetError("K-Dense central-only environment is invalid")
    if rejected != ("registry", "fusion", "ollama", "subagent", "speech", "credential_mutation"):
        raise RuntimeAssetError("K-Dense rejected paths are invalid")
    return KdenseCatalogContract(costs, limits, provenance, environment, rejected)


def _source_paths(value: JsonValue, label: str) -> tuple[KdenseSourcePath, ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeAssetError(f"{label} list is invalid")
    paths = tuple(
        KdenseSourcePath(
            path=require_text(require_mapping(item, label)["path"], f"{label} path"),
            sha256=require_sha256(require_mapping(item, label)["sha256"], f"{label} sha256"),
        )
        for item in value
    )
    if len({record.path for record in paths}) != len(paths):
        raise RuntimeAssetError(f"{label} list is invalid")
    return paths


def _field_mappings(
    value: JsonValue, expected: tuple[tuple[str, str], ...], label: str
) -> tuple[KdenseFieldMapping, ...]:
    if not isinstance(value, list):
        raise RuntimeAssetError(f"{label} is invalid")
    mappings = tuple(
        KdenseFieldMapping(
            source=require_text(require_mapping(item, label)["source"], f"{label} source"),
            target=require_text(require_mapping(item, label)["target"], f"{label} target"),
        )
        for item in value
    )
    if tuple((mapping.source, mapping.target) for mapping in mappings) != expected:
        raise RuntimeAssetError(f"{label} is invalid")
    return mappings


def _texts(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeAssetError(f"{label} is invalid")
    return tuple(require_text(item, label) for item in value)
