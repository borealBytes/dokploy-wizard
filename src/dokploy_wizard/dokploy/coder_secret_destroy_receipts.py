"""Strict durable journal for Coder secret destruction."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.litellm.catalog_persistence_fs import (
    CatalogPersistenceError,
    atomic_write,
    read_contract_file,
    secure_directory,
)
from dokploy_wizard.state.sync_schema import JsonValue

_FILENAME: Final = "coder-secret-destroy-receipts-v1.json"
DestroyReceiptStatus = Literal["running", "completed", "blocked"]
DestroyStepStatus = Literal["intent", "deleted", "blocked"]


@dataclass(frozen=True, slots=True)
class CoderSecretDestroyReceiptError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderSecretDestroyStep:
    secret_id: str
    secret_name: str
    env_name: str
    description: str
    status: DestroyStepStatus
    pre_metadata_sha256: str
    post_metadata_sha256: str | None
    updated_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "secret_id": self.secret_id,
            "secret_name": self.secret_name,
            "env_name": self.env_name,
            "description": self.description,
            "status": self.status,
            "pre_metadata_sha256": self.pre_metadata_sha256,
            "post_metadata_sha256": self.post_metadata_sha256,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CoderSecretDestroyReceipt:
    owner_id: str
    source_receipt_sha256: str
    status: DestroyReceiptStatus
    steps: tuple[CoderSecretDestroyStep, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "owner_id": self.owner_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
        }


class CoderSecretDestroyReceiptStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    def load(self) -> CoderSecretDestroyReceipt | None:
        self._secure_directory()
        try:
            payload = read_contract_file(self._path, "Coder secret destroy receipt")
        except CatalogPersistenceError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt is unavailable"
            ) from error
        if payload is None:
            return None
        try:
            value: JsonValue = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt is malformed"
            ) from error
        from dokploy_wizard.dokploy.coder_secret_destroy_receipt_validation import (
            parse_destroy_receipt,
        )

        return parse_destroy_receipt(value)

    def write(self, receipt: CoderSecretDestroyReceipt) -> None:
        from dokploy_wizard.dokploy.coder_secret_destroy_receipt_validation import (
            parse_destroy_receipt,
        )

        parse_destroy_receipt(receipt.to_dict())
        try:
            atomic_write(self._path, canonical_destroy_receipt_bytes(receipt))
        except CatalogPersistenceError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt cannot be persisted"
            ) from error

    def remove(self, receipt: CoderSecretDestroyReceipt) -> None:
        self._secure_directory()
        expected = canonical_destroy_receipt_bytes(receipt)
        try:
            current = read_contract_file(self._path, "Coder secret destroy receipt")
        except CatalogPersistenceError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt is unavailable"
            ) from error
        if current != expected:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt changed before removal"
            )
        try:
            self._path.unlink()
            _fsync_directory(self._state_dir)
        except OSError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt cannot be removed"
            ) from error

    @property
    def _path(self) -> Path:
        return self._state_dir / _FILENAME

    def _secure_directory(self) -> None:
        try:
            secure_directory(self._state_dir)
        except CatalogPersistenceError as error:
            raise CoderSecretDestroyReceiptError(
                "Coder secret destroy receipt directory is invalid"
            ) from error


def canonical_destroy_receipt_bytes(receipt: CoderSecretDestroyReceipt) -> bytes:
    return json.dumps(
        receipt.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
