"""Versioned immutable evidence for a Task 1 external proof context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    sha256,
)

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | Mapping[str, "JsonValue"]
_FIELDS: Final = (
    "context_sha256",
    "context_id_sha256",
    "namespace_sha256",
    "uploaded_env_sha256",
    "source_env_sha256",
    "source_env_mode",
    "source_path",
    "source_path_sha256",
    "proof_directory",
    "proof_directory_sha256",
    "upload_env_path",
    "upload_env_path_sha256",
    "upload_env_mode",
    "context_path",
    "context_path_sha256",
    "context_mode",
    "seed_path",
    "seed_path_sha256",
    "seed_sha256",
    "seed_mode",
    "receipt_path",
    "receipt_path_sha256",
    "overlay_receipt_sha256",
    "receipt_mode",
    "expected_restored_source_sha256",
    "observed_restored_source_sha256",
    "observed_restored_source_mode",
)
_HASH_FIELDS: Final = frozenset(field for field in _FIELDS if field.endswith("sha256"))
_REQUIRED_HASH_FIELDS: Final = _HASH_FIELDS - {"observed_restored_source_sha256"}
_PATH_FIELDS: Final = frozenset(
    {"proof_directory", *(field for field in _FIELDS if field.endswith("_path"))}
)
_MODE_FIELDS: Final = frozenset(field for field in _FIELDS if field.endswith("_mode"))


@dataclass(frozen=True, slots=True)
class Task1ProofContextEvidenceV1:
    """Hash-bound external inputs and the observed canonical-source restoration."""

    context_sha256: str
    context_id_sha256: str
    namespace_sha256: str
    uploaded_env_sha256: str
    source_env_sha256: str
    source_env_mode: int
    source_path: str
    source_path_sha256: str
    proof_directory: str
    proof_directory_sha256: str
    upload_env_path: str
    upload_env_path_sha256: str
    upload_env_mode: int
    context_path: str
    context_path_sha256: str
    context_mode: int
    seed_path: str
    seed_path_sha256: str
    seed_sha256: str
    seed_mode: int
    receipt_path: str
    receipt_path_sha256: str
    overlay_receipt_sha256: str
    receipt_mode: int
    expected_restored_source_sha256: str
    observed_restored_source_sha256: str | None
    observed_restored_source_mode: int | None

    def to_payload(self) -> dict[str, JsonValue]:
        return {"schema_version": 1, **{field: getattr(self, field) for field in _FIELDS}}

    @classmethod
    def from_payload(cls, value: Mapping[str, JsonValue]) -> "Task1ProofContextEvidenceV1":
        expected = {"schema_version", *_FIELDS}
        if set(value) != expected or value.get("schema_version") != 1:
            raise Task1ProofContextError("Task 1 proof context evidence schema is invalid")
        strings = _REQUIRED_HASH_FIELDS | _PATH_FIELDS
        if any(not isinstance(value[field], str) for field in strings):
            raise Task1ProofContextError("Task 1 proof context evidence fields are invalid")
        modes = _MODE_FIELDS - {"observed_restored_source_mode"}
        if any(
            isinstance(value[field], bool) or not isinstance(value[field], int) for field in modes
        ):
            raise Task1ProofContextError("Task 1 proof context evidence modes are invalid")
        observed_hash = value["observed_restored_source_sha256"]
        observed_mode = value["observed_restored_source_mode"]
        if (observed_hash is None) != (observed_mode is None):
            raise Task1ProofContextError("Task 1 proof restoration evidence is incomplete")
        if observed_hash is not None and (
            not isinstance(observed_hash, str)
            or isinstance(observed_mode, bool)
            or not isinstance(observed_mode, int)
        ):
            raise Task1ProofContextError("Task 1 proof restoration evidence is invalid")
        evidence = cls(
            _text(value["context_sha256"]),
            _text(value["context_id_sha256"]),
            _text(value["namespace_sha256"]),
            _text(value["uploaded_env_sha256"]),
            _text(value["source_env_sha256"]),
            _mode(value["source_env_mode"]),
            _text(value["source_path"]),
            _text(value["source_path_sha256"]),
            _text(value["proof_directory"]),
            _text(value["proof_directory_sha256"]),
            _text(value["upload_env_path"]),
            _text(value["upload_env_path_sha256"]),
            _mode(value["upload_env_mode"]),
            _text(value["context_path"]),
            _text(value["context_path_sha256"]),
            _mode(value["context_mode"]),
            _text(value["seed_path"]),
            _text(value["seed_path_sha256"]),
            _text(value["seed_sha256"]),
            _mode(value["seed_mode"]),
            _text(value["receipt_path"]),
            _text(value["receipt_path_sha256"]),
            _text(value["overlay_receipt_sha256"]),
            _mode(value["receipt_mode"]),
            _text(value["expected_restored_source_sha256"]),
            _optional_text(observed_hash),
            _optional_mode(observed_mode),
        )
        validate_task1_context_evidence(evidence)
        return evidence


def validate_task1_context_evidence(evidence: Task1ProofContextEvidenceV1) -> None:
    """Validate values before evidence becomes durable guard or result state."""

    hashes = tuple(getattr(evidence, field) for field in _REQUIRED_HASH_FIELDS)
    if any(not _valid_hash(value) for value in hashes):
        raise Task1ProofContextError("Task 1 proof context evidence hash is invalid")
    observed_hash = evidence.observed_restored_source_sha256
    if observed_hash is not None and not _valid_hash(observed_hash):
        raise Task1ProofContextError("Task 1 proof restoration hash is invalid")
    paths = tuple(getattr(evidence, field) for field in _PATH_FIELDS)
    if any(not Path(value).is_absolute() for value in paths):
        raise Task1ProofContextError("Task 1 proof context evidence path is invalid")
    for path_field in _PATH_FIELDS:
        path = getattr(evidence, path_field)
        if sha256(path.encode()) != getattr(evidence, f"{path_field}_sha256"):
            raise Task1ProofContextError("Task 1 proof context evidence path drifted")
    modes = tuple(getattr(evidence, field) for field in _MODE_FIELDS)
    if any(
        isinstance(mode, bool) or not isinstance(mode, int) or not 1 <= mode <= 0o777
        for mode in modes
        if mode is not None
    ):
        raise Task1ProofContextError("Task 1 proof context evidence mode is invalid")
    external_modes = _MODE_FIELDS - {"source_env_mode", "observed_restored_source_mode"}
    if any(getattr(evidence, field) != 0o600 for field in external_modes):
        raise Task1ProofContextError("Task 1 proof external file mode is invalid")
    if evidence.source_env_sha256 != evidence.expected_restored_source_sha256:
        raise Task1ProofContextError("Task 1 proof source and restoration hashes differ")
    if evidence.observed_restored_source_sha256 is not None and (
        evidence.observed_restored_source_sha256 != evidence.expected_restored_source_sha256
        or evidence.observed_restored_source_mode != evidence.source_env_mode
    ):
        raise Task1ProofContextError("Task 1 proof restoration evidence drifted")


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and not (set(value) - set("0123456789abcdef"))


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise Task1ProofContextError("Task 1 proof context evidence field is invalid")
    return value


def _mode(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Task1ProofContextError("Task 1 proof context evidence mode is invalid")
    return value


def _optional_text(value: JsonValue) -> str | None:
    return None if value is None else _text(value)


def _optional_mode(value: JsonValue) -> int | None:
    return None if value is None else _mode(value)
