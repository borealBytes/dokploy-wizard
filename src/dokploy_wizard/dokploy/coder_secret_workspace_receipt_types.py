from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MAX_CREATE_ATTEMPTS: Final = 2


class WorkspaceVerificationPhase(StrEnum):
    PLANNED = "planned"
    CREATED = "created"
    READY = "ready"
    HASHED = "hashed"
    DELETING = "deleting"
    DELETED = "deleted"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkspaceVerificationReceipt:
    owner_id: str
    workspace_id: str | None
    workspace_name: str
    workspace_owner_id: str | None
    workspace_owner_name: str | None
    template_id: str
    template_name: str
    env_name: str
    expected_value_sha256: str
    observed_value_sha256: str | None
    create_attempts: int
    phase: WorkspaceVerificationPhase
    failure_reason: str | None
    created_at: str
    updated_at: str
    protocol_revision: int = 1
    predecessor_receipt_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceVerificationPlan:
    owner_id: str
    workspace_name: str
    template_id: str
    template_name: str
    env_name: str
    expected_value_sha256: str
