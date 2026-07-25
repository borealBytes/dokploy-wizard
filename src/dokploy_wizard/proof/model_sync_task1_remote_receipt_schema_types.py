"""Finite value types for Task 1 remote proof receipts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Task1RemoteReceiptError(RuntimeError):
    """Raised when remote proof evidence is incomplete, foreign, or malformed."""


class Task1RemoteProofStage(StrEnum):
    ARCHIVE = "archive"
    UPLOAD = "upload"
    INSTALL = "install"
    VERIFY = "verify"
    INSPECT = "inspect"
    COLLECT = "collect"


class Task1RemoteProofResult(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"


TASK1_REMOTE_PROOF_STAGES: Final = tuple(Task1RemoteProofStage)


@dataclass(frozen=True, slots=True)
class Task1RemoteProofExpectation:
    proof_commit: str
    context_sha256: str
    uploaded_env_sha256: str


@dataclass(frozen=True, slots=True)
class Task1RemoteProofBinding:
    proof_commit: str
    context_sha256: str
    uploaded_env_sha256: str
    archive_sha256: str
    command_mode: str = "proof"
    expected_terminal_stage: str = "collect"


@dataclass(frozen=True, slots=True)
class Task1RemoteProofStageRecord:
    sequence: int
    stage: Task1RemoteProofStage
    evidence_sha256: str
    chain_sha256: str
