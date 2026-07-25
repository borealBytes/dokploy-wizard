"""Canonical hashing primitives for Task 1 remote proof receipts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Final

from dokploy_wizard.proof import JsonValue
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofBinding,
    Task1RemoteProofStage,
    Task1RemoteReceiptError,
)

SHA256_PATTERN: Final = re.compile(r"[a-f0-9]{64}")
COMMIT_PATTERN: Final = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


def canonical_receipt_bytes(value: dict[str, JsonValue]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def require_sha256(value: str) -> None:
    if SHA256_PATTERN.fullmatch(value) is None:
        raise Task1RemoteReceiptError("Task 1 remote proof hash is invalid")


def binding_sha256(binding: Task1RemoteProofBinding) -> str:
    return hashlib.sha256(
        canonical_receipt_bytes(
            {
                "archive_sha256": binding.archive_sha256,
                "command_mode": binding.command_mode,
                "context_sha256": binding.context_sha256,
                "expected_terminal_stage": binding.expected_terminal_stage,
                "proof_commit": binding.proof_commit,
                "uploaded_env_sha256": binding.uploaded_env_sha256,
            }
        )
    ).hexdigest()


def stage_chain_sha256(
    previous: str,
    sequence: int,
    stage: Task1RemoteProofStage,
    evidence_sha256: str,
) -> str:
    return hashlib.sha256(
        canonical_receipt_bytes(
            {
                "evidence_sha256": evidence_sha256,
                "previous_sha256": previous,
                "sequence": sequence,
                "stage": str(stage),
            }
        )
    ).hexdigest()
