from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.litellm.catalog_persistence_fs import (
    CatalogPersistenceError,
    atomic_write,
    read_contract_file,
    secure_directory,
)
from dokploy_wizard.state.sync_schema import JsonValue

_FILENAME: Final = "coder-secret-receipts-v1.json"

ReceiptStatus = Literal["planned", "running", "blocked", "completed", "failed"]
SecretOperation = Literal["create", "update", "noop"]
StepStatus = Literal["intent", "submitted", "verified", "blocked"]
ReceiptErrorKind = Literal["directory", "file", "json", "schema"]


@dataclass(frozen=True, slots=True)
class CoderSecretReceiptError(ValueError):
    reason: str
    kind: ReceiptErrorKind = "schema"

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderSecretReceiptStep:
    secret_name: str
    secret_id: str | None
    env_name: str
    description: str
    operation: SecretOperation
    status: StepStatus
    pre_metadata_sha256: str | None
    second_pre_metadata_sha256: str | None
    source_value_sha256: str
    expected_post_sha256: str
    response_sha256: str | None
    workspace_verification_sha256: str | None
    updated_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "secret_name": self.secret_name,
            "secret_id": self.secret_id,
            "env_name": self.env_name,
            "description": self.description,
            "operation": self.operation,
            "status": self.status,
            "pre_metadata_sha256": self.pre_metadata_sha256,
            "second_pre_metadata_sha256": self.second_pre_metadata_sha256,
            "source_value_sha256": self.source_value_sha256,
            "expected_post_sha256": self.expected_post_sha256,
            "response_sha256": self.response_sha256,
            "workspace_verification_sha256": self.workspace_verification_sha256,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CoderSecretReceipt:
    owner_id: str
    status: ReceiptStatus
    steps: tuple[CoderSecretReceiptStep, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "owner_id": self.owner_id,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
        }


class CoderSecretReceiptStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    def load(self) -> CoderSecretReceipt | None:
        self._secure_directory()
        try:
            payload = read_contract_file(self._path, "Coder secret receipt")
        except CatalogPersistenceError as error:
            raise CoderSecretReceiptError(
                "Coder secret receipt is unavailable", kind="file"
            ) from error
        if payload is None:
            return None
        try:
            value: JsonValue = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CoderSecretReceiptError(
                "Coder secret receipt is malformed", kind="json"
            ) from error
        return parse_receipt(value)

    def write(self, receipt: CoderSecretReceipt) -> None:
        try:
            atomic_write(self._path, canonical_receipt_bytes(receipt))
        except CatalogPersistenceError as error:
            raise CoderSecretReceiptError("Coder secret receipt cannot be persisted") from error

    def remove(self, receipt: CoderSecretReceipt) -> None:
        self._secure_directory()
        expected = canonical_receipt_bytes(receipt)
        try:
            current = read_contract_file(self._path, "Coder secret receipt")
        except CatalogPersistenceError as error:
            raise CoderSecretReceiptError("Coder secret receipt is unavailable") from error
        if current != expected:
            raise CoderSecretReceiptError("Coder secret receipt changed before removal")
        try:
            self._path.unlink()
            _fsync_directory(self._state_dir)
        except OSError as error:
            raise CoderSecretReceiptError("Coder secret receipt cannot be removed") from error

    @property
    def _path(self) -> Path:
        return self._state_dir / _FILENAME

    def _secure_directory(self) -> None:
        try:
            secure_directory(self._state_dir)
        except CatalogPersistenceError as error:
            raise CoderSecretReceiptError(
                "Coder secret receipt directory is invalid", kind="directory"
            ) from error


def canonical_receipt_bytes(receipt: CoderSecretReceipt) -> bytes:
    return json.dumps(
        receipt.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def metadata_sha256(payload: dict[str, JsonValue]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_receipt(value: JsonValue) -> CoderSecretReceipt:
    from dokploy_wizard.dokploy.coder_secret_receipt_validation import parse_receipt as validate

    return validate(value)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
