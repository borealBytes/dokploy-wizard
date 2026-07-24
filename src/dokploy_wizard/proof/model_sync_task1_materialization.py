"""Pure planning and durable publication of Task 1 external proof files."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    Task1ProofContextV1,
    canonical_json_bytes,
    sha256,
    task1_receipt_payload,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
)
from dokploy_wizard.proof.model_sync_task1_evidence_validation import (
    verify_task1_external_evidence,
)
from dokploy_wizard.proof.model_sync_task1_partial_cleanup import (
    inspect_task1_partial_materialization,
)

_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_EXPECTED_FILES: Final = (
    "upload.env",
    "task1-proof-context.json",
    "task1-proof-context.seed",
    "task1-proof-context.receipt.json",
)


class Task1PreparationBoundary(StrEnum):
    ENV_INTENT_RECORDED = "env-intent-recorded"
    PROOF_DIRECTORY_PUBLISHED = "proof-directory-published"
    UPLOAD_ENV_PUBLISHED = "upload-env-published"
    CONTEXT_PUBLISHED = "context-published"
    SEED_PUBLISHED = "seed-published"
    RECEIPT_PUBLISHED = "receipt-published"
    BACKUP_PUBLISHED = "backup-published"
    BEFORE_PROOF_ACTIVE = "before-proof-active"
    PROOF_ACTIVE_RECORDED = "proof-active-recorded"


Task1PreparationHook = Callable[[Task1PreparationBoundary], None]


@dataclass(frozen=True, slots=True)
class Task1ProofPlanInputs:
    context: Task1ProofContextV1
    source_path: Path
    proof_directory: Path
    uploaded_env_bytes: bytes


@dataclass(frozen=True, slots=True)
class Task1ProofExternalPlan:
    proof_directory: Path
    uploaded_env_file: Path
    context_file: Path
    seed_file: Path
    receipt_file: Path
    evidence: Task1ProofContextEvidenceV1
    contents: tuple[tuple[Path, bytes], ...]


def ignore_task1_preparation_boundary(_boundary: Task1PreparationBoundary) -> None:
    return


def task1_overlay(token: str) -> dict[str, str]:
    return {
        "STACK_NAME": f"task1-{token}",
        "CLOUDFLARE_TUNNEL_NAME": f"task1-{token}-tunnel",
        "DOKPLOY_SUBDOMAIN": f"dp-{token}",
        "CODER_SUBDOMAIN": f"cd-{token}",
        "SEAWEEDFS_SUBDOMAIN": f"s3-{token}",
        "LITELLM_ADMIN_SUBDOMAIN": f"lm-{token}",
        "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID": token,
        "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD": "true",
    }


def plan_task1_external_files(inputs: Task1ProofPlanInputs) -> Task1ProofExternalPlan:
    context = inputs.context
    directory = inputs.proof_directory
    upload = directory / _EXPECTED_FILES[0]
    context_file = directory / _EXPECTED_FILES[1]
    seed = directory / _EXPECTED_FILES[2]
    receipt = directory / _EXPECTED_FILES[3]
    context_bytes = context.to_bytes()
    seed_bytes = f"{context.context_id}\n".encode()
    source_path_sha256 = sha256(str(inputs.source_path.resolve()).encode())
    receipt_bytes = canonical_json_bytes(task1_receipt_payload(context, source_path_sha256))
    contents = (
        (upload, inputs.uploaded_env_bytes),
        (context_file, context_bytes),
        (seed, seed_bytes),
        (receipt, receipt_bytes),
    )
    evidence = _evidence(inputs, contents, source_path_sha256)
    return Task1ProofExternalPlan(
        directory, upload, context_file, seed, receipt, evidence, contents
    )


def materialize_task1_external_files(
    plan: Task1ProofExternalPlan,
    *,
    boundary_hook: Task1PreparationHook = ignore_task1_preparation_boundary,
) -> None:
    _create_or_verify_directory(plan.proof_directory)
    boundary_hook(Task1PreparationBoundary.PROOF_DIRECTORY_PUBLISHED)
    inspect_task1_partial_materialization(plan.evidence)
    boundaries = (
        Task1PreparationBoundary.UPLOAD_ENV_PUBLISHED,
        Task1PreparationBoundary.CONTEXT_PUBLISHED,
        Task1PreparationBoundary.SEED_PUBLISHED,
        Task1PreparationBoundary.RECEIPT_PUBLISHED,
    )
    for (path, content), boundary in zip(plan.contents, boundaries, strict=True):
        artifacts.write_or_verify_exact_bytes(path, content)
        boundary_hook(boundary)
    verify_task1_external_evidence(plan.evidence)


def _create_or_verify_directory(path: Path) -> None:
    path.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    parent = os.lstat(path.parent)
    if not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != _DIRECTORY_MODE:
        raise Task1ProofContextError("Task 1 proof parent is not a mode-0700 directory")
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, _DIRECTORY_MODE)
        artifacts._fsync_directory(path)
        artifacts._fsync_directory(path.parent)
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE:
        raise Task1ProofContextError("Task 1 proof directory is not a mode-0700 directory")


def _evidence(
    inputs: Task1ProofPlanInputs,
    contents: tuple[tuple[Path, bytes], ...],
    source_path_sha256: str,
) -> Task1ProofContextEvidenceV1:
    context = inputs.context

    def resolved(value: Path) -> str:
        return str(value.resolve())

    upload, context_file, seed, receipt = (path for path, _content in contents)
    seed_bytes = contents[2][1]
    receipt_bytes = contents[3][1]
    return Task1ProofContextEvidenceV1(
        sha256(context.to_bytes()),
        sha256(context.context_id.encode()),
        context.namespace_sha256,
        context.uploaded_env_sha256,
        context.source_env_sha256,
        context.source_env_mode,
        resolved(inputs.source_path),
        source_path_sha256,
        resolved(inputs.proof_directory),
        sha256(resolved(inputs.proof_directory).encode()),
        resolved(upload),
        sha256(resolved(upload).encode()),
        _FILE_MODE,
        resolved(context_file),
        sha256(resolved(context_file).encode()),
        _FILE_MODE,
        resolved(seed),
        sha256(resolved(seed).encode()),
        sha256(seed_bytes),
        _FILE_MODE,
        resolved(receipt),
        sha256(resolved(receipt).encode()),
        sha256(receipt_bytes),
        _FILE_MODE,
        context.expected_restored_source_sha256,
        None,
        None,
    )
