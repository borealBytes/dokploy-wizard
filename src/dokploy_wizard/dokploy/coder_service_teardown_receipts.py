"""Closed-schema journal for Coder service teardown and receipt finalization."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from dokploy_wizard.litellm.catalog_persistence_fs import (
    CatalogPersistenceError,
    atomic_write,
    read_contract_file,
    secure_directory,
)
from dokploy_wizard.state.sync_schema import JsonValue

_FILENAME: Final = "coder-service-teardown-receipt-v1.json"
_RECEIPT_KEYS: Final = frozenset(
    (
        "schema_version",
        "mode",
        "stack_name",
        "resource_type",
        "resource_id",
        "resource_scope",
        "owner_id",
        "source_receipt_sha256",
        "destroy_receipt_sha256",
        "workspace_receipt_sha256",
        "phase",
        "updated_at",
    )
)

CoderServiceTeardownPhase = Literal[
    "intent",
    "service_deleted",
    "source_removed",
    "destroy_removed",
    "workspace_removed",
    "finalized",
]
_PHASES: Final[dict[str, CoderServiceTeardownPhase]] = {
    "intent": "intent",
    "service_deleted": "service_deleted",
    "source_removed": "source_removed",
    "destroy_removed": "destroy_removed",
    "workspace_removed": "workspace_removed",
    "finalized": "finalized",
}


@dataclass(frozen=True, slots=True)
class CoderServiceTeardownReceiptError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderServiceTeardownReceipt:
    stack_name: str
    resource_type: str
    resource_id: str
    resource_scope: str
    owner_id: str
    source_receipt_sha256: str
    destroy_receipt_sha256: str
    workspace_receipt_sha256: str | None
    phase: CoderServiceTeardownPhase
    updated_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "mode": "destroy",
            "stack_name": self.stack_name,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_scope": self.resource_scope,
            "owner_id": self.owner_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "destroy_receipt_sha256": self.destroy_receipt_sha256,
            "workspace_receipt_sha256": self.workspace_receipt_sha256,
            "phase": self.phase,
            "updated_at": self.updated_at,
        }


class CoderServiceTeardownReceiptStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    def load(self) -> CoderServiceTeardownReceipt | None:
        self._secure_directory()
        try:
            payload = read_contract_file(self._path, "Coder service teardown receipt")
        except CatalogPersistenceError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt is unavailable"
            ) from error
        if payload is None:
            return None
        try:
            value: JsonValue = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt is malformed"
            ) from error
        return parse_service_teardown_receipt(value)

    def write(self, receipt: CoderServiceTeardownReceipt) -> None:
        parse_service_teardown_receipt(receipt.to_dict())
        try:
            atomic_write(self._path, canonical_service_teardown_receipt_bytes(receipt))
        except CatalogPersistenceError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt cannot be persisted"
            ) from error

    def remove(self, receipt: CoderServiceTeardownReceipt) -> None:
        self._secure_directory()
        expected = canonical_service_teardown_receipt_bytes(receipt)
        try:
            current = read_contract_file(self._path, "Coder service teardown receipt")
        except CatalogPersistenceError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt is unavailable"
            ) from error
        if current != expected:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt changed before removal"
            )
        try:
            self._path.unlink()
            _fsync_directory(self._state_dir)
        except OSError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt cannot be removed"
            ) from error

    @property
    def _path(self) -> Path:
        return self._state_dir / _FILENAME

    def _secure_directory(self) -> None:
        try:
            secure_directory(self._state_dir)
        except CatalogPersistenceError as error:
            raise CoderServiceTeardownReceiptError(
                "Coder service teardown receipt directory is invalid"
            ) from error


def canonical_service_teardown_receipt_bytes(
    receipt: CoderServiceTeardownReceipt,
) -> bytes:
    return json.dumps(
        receipt.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def parse_service_teardown_receipt(value: JsonValue) -> CoderServiceTeardownReceipt:
    if not isinstance(value, dict) or frozenset(value) != _RECEIPT_KEYS:
        raise CoderServiceTeardownReceiptError(
            "Coder service teardown receipt has unknown or missing fields"
        )
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise CoderServiceTeardownReceiptError(
            "Coder service teardown receipt version is unsupported"
        )
    if value["mode"] != "destroy":
        raise CoderServiceTeardownReceiptError("Coder service teardown mode is invalid")
    return CoderServiceTeardownReceipt(
        stack_name=_text(value["stack_name"], "stack name"),
        resource_type=_text(value["resource_type"], "resource type"),
        resource_id=_text(value["resource_id"], "resource id"),
        resource_scope=_text(value["resource_scope"], "resource scope"),
        owner_id=_hash(value["owner_id"], "owner id"),
        source_receipt_sha256=_hash(value["source_receipt_sha256"], "source receipt"),
        destroy_receipt_sha256=_hash(value["destroy_receipt_sha256"], "destroy receipt"),
        workspace_receipt_sha256=_nullable_hash(
            value["workspace_receipt_sha256"], "workspace receipt"
        ),
        phase=_phase(value["phase"]),
        updated_at=_timestamp(value["updated_at"]),
    )


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise CoderServiceTeardownReceiptError(
            f"Coder service teardown {label} is invalid"
        )
    return value


def _hash(value: JsonValue, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CoderServiceTeardownReceiptError(
            f"Coder service teardown {label} is invalid"
        )
    return text


def _nullable_hash(value: JsonValue, label: str) -> str | None:
    return None if value is None else _hash(value, label)


def _phase(value: JsonValue) -> CoderServiceTeardownPhase:
    text = _text(value, "phase")
    phase = _PHASES.get(text)
    if phase is None:
        raise CoderServiceTeardownReceiptError("Coder service teardown phase is invalid")
    return phase


def _timestamp(value: JsonValue) -> str:
    text = _text(value, "updated_at")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise CoderServiceTeardownReceiptError(
            "Coder service teardown timestamp is invalid"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise CoderServiceTeardownReceiptError(
            "Coder service teardown timestamp is invalid"
        )
    return text


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CoderServiceTeardownPhase",
    "CoderServiceTeardownReceipt",
    "CoderServiceTeardownReceiptError",
    "CoderServiceTeardownReceiptStore",
    "canonical_service_teardown_receipt_bytes",
]
