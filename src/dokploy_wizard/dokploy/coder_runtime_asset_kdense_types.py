"""Typed Task 7 K-Dense source/build attestation records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KdenseChecksumAsset:
    """One immutable downloadable asset and its SHA-256 digest."""

    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class KdenseArchitectureAssets:
    """One version with immutable amd64 and arm64 artifacts."""

    version: str
    amd64: KdenseChecksumAsset
    arm64: KdenseChecksumAsset


@dataclass(frozen=True, slots=True)
class KdenseSourcePath:
    """A required regular source file relative to the extracted root."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class KdensePatch:
    """One checked-in patch whose order is part of the source contract."""

    name: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class KdenseSkillSource:
    """Immutable scientific-skills provenance and canonical-tree digest."""

    repository: str
    commit: str
    tree: str
    subtree: str
    archive_url: str
    archive_sha256: str
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class KdenseFieldMapping:
    """One source-to-runtime catalog field mapping."""

    source: str
    target: str


@dataclass(frozen=True, slots=True)
class KdenseCatalogContract:
    """Central-only model pricing, limits, and provenance mapping."""

    cost_fields: tuple[KdenseFieldMapping, ...]
    limits: tuple[KdenseFieldMapping, ...]
    provenance: tuple[KdenseFieldMapping, ...]
    central_only_environment: tuple[str, ...]
    rejected_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KdenseRuntimeContract:
    """All immutable Task 7 K-Dense source/build inputs."""

    repository: str
    commit: str
    tree: str
    codeload_url: str
    archive_sha256: str
    archive_root: str
    source_paths: tuple[KdenseSourcePath, ...]
    absent_paths: tuple[str, ...]
    node: KdenseArchitectureAssets
    cpython: KdenseArchitectureAssets
    uv: KdenseArchitectureAssets
    skills: KdenseSkillSource
    patches: tuple[KdensePatch, ...]
    catalog: KdenseCatalogContract
