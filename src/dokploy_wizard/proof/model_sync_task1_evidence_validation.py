"""Fail-closed validation of Task 1 persistent external proof material."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    Task1ProofContextV1,
    canonical_json_bytes,
    sha256,
    task1_env_bytes,
    task1_receipt_payload,
    validate_task1_uploaded_values,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
    validate_task1_context_evidence,
)

_DIRECTORY_MODE: Final = 0o700
_EXPECTED_FILES: Final = (
    "upload.env",
    "task1-proof-context.json",
    "task1-proof-context.seed",
    "task1-proof-context.receipt.json",
)
_MAX_FILE_BYTES: Final = 256 * 1024


def verify_task1_external_evidence(evidence: Task1ProofContextEvidenceV1) -> None:
    """Reject external proof file, mode, path, context, or upload drift before use."""

    validate_task1_context_evidence(evidence)
    directory = Path(evidence.proof_directory)
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE:
        raise Task1ProofContextError("Task 1 proof directory is not a mode-0700 directory")
    entries = tuple(sorted(entry.name for entry in os.scandir(directory)))
    if entries != tuple(sorted(_EXPECTED_FILES)):
        raise Task1ProofContextError("Task 1 proof directory contains unexpected files")
    files = {
        "upload": (
            Path(evidence.upload_env_path),
            evidence.uploaded_env_sha256,
            evidence.upload_env_mode,
        ),
        "context": (Path(evidence.context_path), evidence.context_sha256, evidence.context_mode),
        "seed": (Path(evidence.seed_path), evidence.seed_sha256, evidence.seed_mode),
        "receipt": (
            Path(evidence.receipt_path),
            evidence.overlay_receipt_sha256,
            evidence.receipt_mode,
        ),
    }
    contents: dict[str, bytes] = {}
    for name, (path, digest, mode) in files.items():
        if path.parent != directory:
            raise Task1ProofContextError("Task 1 proof external path escaped its directory")
        content = read_task1_evidence_file(path, mode)
        if sha256(content) != digest:
            raise Task1ProofContextError("Task 1 proof external file drifted")
        contents[name] = content
    _validate_context_upload_pair(evidence, contents)


def read_task1_evidence_file(path: Path, mode: int | None) -> bytes:
    try:
        return proof.read_bounded_regular_bytes(path, _MAX_FILE_BYTES, mode)[0]
    except (OSError, ValueError) as error:
        raise Task1ProofContextError("Task 1 proof file is unreadable") from error


def _validate_context_upload_pair(
    evidence: Task1ProofContextEvidenceV1, contents: dict[str, bytes]
) -> None:
    context = Task1ProofContextV1.from_bytes(contents["context"])
    if (
        evidence.context_id_sha256 != sha256(context.context_id.encode())
        or evidence.namespace_sha256 != context.namespace_sha256
        or evidence.uploaded_env_sha256 != context.uploaded_env_sha256
        or evidence.source_env_sha256 != context.source_env_sha256
        or evidence.source_env_mode != context.source_env_mode
        or evidence.expected_restored_source_sha256 != context.expected_restored_source_sha256
        or contents["seed"] != f"{context.context_id}\n".encode()
    ):
        raise Task1ProofContextError("Task 1 proof external context does not match its evidence")
    validate_task1_uploaded_values(context, _parse_upload_env(contents["upload"]))
    expected_receipt = canonical_json_bytes(
        task1_receipt_payload(context, evidence.source_path_sha256)
    )
    if contents["receipt"] != expected_receipt:
        raise Task1ProofContextError("Task 1 proof overlay receipt does not match its context")


def _parse_upload_env(content: bytes) -> dict[str, str]:
    try:
        lines = content.decode().splitlines()
    except UnicodeDecodeError as error:
        raise Task1ProofContextError("Task 1 proof upload is not UTF-8") from error
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator == "" or key == "" or key in values:
            raise Task1ProofContextError("Task 1 proof upload format is invalid")
        values[key] = value
    if task1_env_bytes(values) != content:
        raise Task1ProofContextError("Task 1 proof upload is not canonical")
    return values
