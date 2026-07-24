"""Strict canonical schema for the private Task 1 Cloudflare write journal."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_parsing import optional_error
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError

JOURNAL_SCHEMA_VERSION: Final = 2
JOURNAL_MAX_BYTES: Final = 256 * 1024
_HASH = re.compile(r"^[a-f0-9]{64}$")
_LOGICAL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,47}:[a-f0-9]{64}$")


class JournalStatus(StrEnum):
    ACTIVE = "active"
    CLEANED = "cleaned"
    RESTORED = "restored"


class JournalOperationKind(StrEnum):
    TUNNEL = "tunnel"
    DNS_RECORD = "dns_record"
    ACCESS_APPLICATION = "access_application"
    ACCESS_POLICY = "access_policy"
    TUNNEL_CONFIGURATION = "tunnel_configuration"


class JournalOperationState(StrEnum):
    INTENT = "intent"
    ABORTED = "aborted"
    CREATED = "created"
    CLEANED = "cleaned"


class JournalCleanupState(StrEnum):
    PENDING = "pending"
    VERIFIED_ABSENT = "verified_absent"
    DELETED_WITH_TUNNEL = "deleted_with_tunnel"


@dataclass(frozen=True, slots=True)
class Task1CloudflareJournalVersion:
    generation: int
    cas_token: str


@dataclass(frozen=True, slots=True)
class Task1CloudflareJournalIntent:
    logical_key: str
    kind: JournalOperationKind
    account_id: str
    zone_id: str | None
    parent_id: str | None
    expected_name_sha256: str | None
    expected_domain_sha256: str | None
    pre_absence_sha256: str
    desired_spec_sha256: str


@dataclass(frozen=True, slots=True)
class Task1CloudflareJournalOperation:
    sequence: int
    intent: Task1CloudflareJournalIntent
    state: JournalOperationState
    response_id: str | None
    post_fingerprint_sha256: str | None
    error: str | None
    cleanup_state: JournalCleanupState | None

    def to_payload(self) -> dict[str, object]:
        return {
            "account_id": self.intent.account_id,
            "cleanup_state": self.cleanup_state,
            "error": self.error,
            "expected_domain_sha256": self.intent.expected_domain_sha256,
            "expected_name_sha256": self.intent.expected_name_sha256,
            "kind": self.intent.kind,
            "logical_key": self.intent.logical_key,
            "parent_id": self.intent.parent_id,
            "post_fingerprint_sha256": self.post_fingerprint_sha256,
            "pre_absence_sha256": self.intent.pre_absence_sha256,
            "response_id": self.response_id,
            "sequence": self.sequence,
            "state": self.state,
            "desired_spec_sha256": self.intent.desired_spec_sha256,
            "zone_id": self.intent.zone_id,
        }


@dataclass(frozen=True, slots=True)
class Task1CloudflareJournalDocument:
    context_sha256: str
    version: Task1CloudflareJournalVersion
    status: JournalStatus
    operations: tuple[Task1CloudflareJournalOperation, ...]

    def to_bytes(self) -> bytes:
        payload = {
            "cas_token": self.version.cas_token,
            "context_sha256": self.context_sha256,
            "generation": self.version.generation,
            "operations": [operation.to_payload() for operation in self.operations],
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "status": self.status,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"

    @classmethod
    def from_bytes(cls, content: bytes) -> "Task1CloudflareJournalDocument":
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Task1ProofContextError(
                "Task 1 Cloudflare cleanup journal is unreadable"
            ) from error
        if not isinstance(decoded, dict):
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
        expected = {
            "cas_token",
            "context_sha256",
            "generation",
            "operations",
            "schema_version",
            "status",
        }
        if set(decoded) != expected or decoded["schema_version"] != JOURNAL_SCHEMA_VERSION:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
        context_sha256 = _require_hash(decoded["context_sha256"])
        cas_token = _require_hash(decoded["cas_token"])
        generation = decoded["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
        try:
            status = JournalStatus(decoded["status"])
        except ValueError as error:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid") from error
        raw_operations = decoded["operations"]
        if not isinstance(raw_operations, list):
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
        operations = tuple(_parse_operation(raw) for raw in raw_operations)
        document = cls(
            context_sha256=context_sha256,
            version=Task1CloudflareJournalVersion(generation, cas_token),
            status=status,
            operations=operations,
        )
        from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_validation import (
            validate_document,
        )

        validate_document(document)
        if content != document.to_bytes():
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is not canonical")
        return document


def _parse_operation(raw: object) -> Task1CloudflareJournalOperation:
    if not isinstance(raw, dict):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    expected = {
        "account_id",
        "cleanup_state",
        "error",
        "expected_domain_sha256",
        "expected_name_sha256",
        "kind",
        "logical_key",
        "parent_id",
        "post_fingerprint_sha256",
        "pre_absence_sha256",
        "response_id",
        "sequence",
        "state",
        "desired_spec_sha256",
        "zone_id",
    }
    if set(raw) != expected:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    sequence = raw["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    try:
        kind = JournalOperationKind(raw["kind"])
        state = JournalOperationState(raw["state"])
        cleanup_state = _optional_cleanup_state(raw["cleanup_state"])
    except ValueError as error:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid") from error
    intent = Task1CloudflareJournalIntent(
        logical_key=_require_logical_key(raw["logical_key"]),
        kind=kind,
        account_id=_require_identifier(raw["account_id"]),
        zone_id=_optional_identifier(raw["zone_id"]),
        parent_id=_optional_identifier(raw["parent_id"]),
        expected_name_sha256=_optional_hash(raw["expected_name_sha256"]),
        expected_domain_sha256=_optional_hash(raw["expected_domain_sha256"]),
        pre_absence_sha256=_require_hash(raw["pre_absence_sha256"]),
        desired_spec_sha256=_require_hash(raw["desired_spec_sha256"]),
    )
    return Task1CloudflareJournalOperation(
        sequence=sequence,
        intent=intent,
        state=state,
        response_id=_optional_identifier(raw["response_id"]),
        post_fingerprint_sha256=_optional_hash(raw["post_fingerprint_sha256"]),
        error=optional_error(raw["error"]),
        cleanup_state=cleanup_state,
    )


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    return value


def _optional_hash(value: object) -> str | None:
    return None if value is None else _require_hash(value)


def _require_identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    return value


def _optional_identifier(value: object) -> str | None:
    return None if value is None else _require_identifier(value)


def _require_logical_key(value: object) -> str:
    if not isinstance(value, str) or _LOGICAL_KEY.fullmatch(value) is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    return value


def _optional_cleanup_state(value: object) -> JournalCleanupState | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    return JournalCleanupState(value)
