"""Typed state for the OpenCode Go static-to-database cutover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

CutoverStatus: TypeAlias = Literal[
    "intent",
    "transitional_deployed",
    "rows_reconciled",
    "visibility_verified",
    "ready_to_cutover",
    "dynamic_deployed",
    "complete",
    "rollback_blocked",
    "failed",
]
CutoverRowOperation: TypeAlias = Literal["noop", "create", "update", "delete"]
CutoverRowStatus: TypeAlias = Literal["intent", "submitted", "verified", "blocked"]
RollbackStatus: TypeAlias = Literal["not_needed", "restored", "blocked"]


class OpenCodeGoCutoverError(RuntimeError):
    """Raised when a cutover receipt or transition is invalid."""


class OpenCodeGoCutoverBlockedError(OpenCodeGoCutoverError):
    """Raised when recovery would need an unsafe database mutation."""


class OpenCodeGoCutoverCrash(OpenCodeGoCutoverError):
    """Test-only crash injection boundary for cutover recovery."""


@dataclass(frozen=True, slots=True)
class CutoverImage:
    compose_sha256: str | None
    config_sha256: str | None
    model_set_sha256: str | None


@dataclass(frozen=True, slots=True)
class CutoverRow:
    model_id: str
    model_name: str
    operation: CutoverRowOperation
    status: CutoverRowStatus
    pre_fingerprint: str | None
    intended_fingerprint: str | None
    current_fingerprint: str | None
    request_sha256: str | None
    response_sha256: str | None
    bootstrap_static: bool
    verified_at: str | None


@dataclass(frozen=True, slots=True)
class CutoverVerification:
    transitional_verified: bool
    visibility_verified: bool
    dynamic_verified: bool
    aliases_sha256: str | None


@dataclass(frozen=True, slots=True)
class CutoverRollback:
    status: RollbackStatus
    pre_fingerprint: str | None
    post_fingerprint: str | None
    current_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class CutoverReceipt:
    operation_id: str
    owner_id: str
    catalog_id: str
    status: CutoverStatus
    compose_id: str
    pre_image: CutoverImage
    transitional_image: CutoverImage
    dynamic_image: CutoverImage
    rows: tuple[CutoverRow, ...]
    verification: CutoverVerification
    rollback: CutoverRollback
    updated_at: str


@dataclass(frozen=True, slots=True)
class CutoverContext:
    owner_id: str
    catalog_id: str
    compose_id: str
    pre_image: CutoverImage
    transitional_image: CutoverImage
    dynamic_image: CutoverImage
    static_aliases: tuple[str, ...]


class OpenCodeGoCutoverDeployment(Protocol):
    def deploy_transitional(self) -> None: ...

    def verify_transitional(self) -> None: ...

    def deploy_dynamic(self) -> None: ...

    def verify_dynamic(self, aliases: tuple[str, ...]) -> None: ...
