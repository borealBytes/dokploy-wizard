"""Typed contracts for workspace model-catalog refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
TargetKind: TypeAlias = Literal["file", "symlink"]
PreState: TypeAlias = Literal["absent", "file", "symlink"]
LegacyScope: TypeAlias = Literal["pointer", "target-and-symlink"]
LegacyShape: TypeAlias = Literal["json-pointer", "yaml-pointer", "json-target-and-symlink"]
TransactionPhase: TypeAlias = Literal[
    "prepared",
    "files_written",
    "pre_switch_verified",
    "switched",
    "processes_stopped",
    "processes_started",
    "health_verified",
    "committed",
    "rollback_started",
    "rolled_back",
    "blocked",
]
ProcessStatus: TypeAlias = Literal[
    "observed", "stop_intent", "stopped", "start_intent", "started", "verified"
]
TargetStatus: TypeAlias = Literal[
    "observed", "prepared", "write_intent", "written", "rollback_intent", "rolled_back"
]
BlockedResolution: TypeAlias = Literal["resume", "rollback"]


class WorkspaceCatalogSyncError(RuntimeError):
    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


class TransactionCasError(WorkspaceCatalogSyncError):
    pass


class TransactionBlockedError(WorkspaceCatalogSyncError):
    pass


class RuntimeIdentityError(WorkspaceCatalogSyncError):
    pass


@dataclass(frozen=True, slots=True)
class OwnedPointer:
    pointer: str
    pre_sha256: str | None
    post_sha256: str


@dataclass(frozen=True, slots=True)
class CatalogTarget:
    path: Path
    kind: TargetKind
    content: bytes
    mode: int
    owned_pointers: tuple[OwnedPointer, ...] = ()

    @classmethod
    def file(cls, *, path: Path, content: bytes, mode: int) -> CatalogTarget:
        return cls(path=path, kind="file", content=content, mode=mode)

    @classmethod
    def symlink(cls, *, path: Path, target: str) -> CatalogTarget:
        return cls(path=path, kind="symlink", content=target.encode("utf-8"), mode=0)


@dataclass(frozen=True, slots=True)
class TargetReceipt:
    path: str
    kind: TargetKind
    pre_state: PreState
    pre_sha256: str | None
    pre_mode: str | None
    pre_target: str | None
    post_mode: str | None
    staged_sha256: str
    post_sha256: str | None
    status: TargetStatus


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    name: str
    pid: int
    start_time_ticks: int
    argv_sha256: str
    executable_sha256: str
    generation: int
    status: ProcessStatus = "observed"


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    generation: int
    cas_token: str
    phase: TransactionPhase
    created_at: str
    updated_at: str
    catalog_sha256: str
    targets: tuple[TargetReceipt, ...]
    processes: tuple[ProcessIdentity, ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class LegacyPointerEvidence:
    workspace_id: str
    template_id: str
    template_version_id: str
    target: str
    pointer: str
    mode: str
    shape: LegacyShape
    scope: LegacyScope
    base_url: str
    credential_value_sha256: str
    pointer_sha256: str
    legacy_renderer_sha256: str
    legacy_exact: bool
    target_sha256: str | None = None
    symlink_state: Literal["present"] | None = None
    symlink_target: str | None = None
    symlink_sha256: str | None = None
    renderer_source_path: str | None = None
    renderer_source_revision: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyAdoptionBinding:
    workspace_id: str
    template_id: str
    template_version_id: str
    target: str
    pointer: str
    mode: str
    shape: LegacyShape
    scope: LegacyScope
    base_url: str
    credential_value_sha256: str


@dataclass(frozen=True, slots=True)
class LegacyAdoptionRequest:
    evidence: LegacyPointerEvidence
    binding: LegacyAdoptionBinding
    current_pre_sha256: str
    current_target_sha256: str


@dataclass(frozen=True, slots=True)
class LegacyAdoptionReceipt:
    schema_version: Literal[1]
    workspace_id: str
    template_id: str
    template_version_id: str
    target: str
    pointer: str
    baseline_sha256: str
    current_pre_sha256: str
    credential_value_sha256: str
    legacy_renderer_sha256: str
    adopted_at: str


@dataclass(frozen=True, slots=True)
class KdenseCatalogMetadata:
    prompt: float
    completion: float
    input_cache_read: float
    input_cache_write: float
    context_length: int
    max_completion_tokens: int
    source_id: str
    merged_decision_sha256: str
    pricing_selection_sha256: str


@dataclass(frozen=True, slots=True)
class CatalogModel:
    alias: str
    display_name: str
    kdense_metadata: KdenseCatalogMetadata | None = None


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    base_url: str
    credential_environment: str
    credential_value_sha256: str
    models: tuple[CatalogModel, ...]


@dataclass(frozen=True, slots=True)
class AdapterPlan:
    targets: tuple[CatalogTarget, ...]
    health_endpoints: tuple[str, ...]
    credential_environment: str
    base_environment: str
