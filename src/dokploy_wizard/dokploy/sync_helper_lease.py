"""Lease receipts, results, releases, freshness, and phase CAS transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, cast

from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    canonical_digest,
    require_digest,
    require_exact_keys,
    require_string,
)

_TRANSITIONS: Final = {
    "created": frozenset({"starting", "failed"}),
    "starting": frozenset({"acquired_held", "failed"}),
    "acquired_held": frozenset({"parent_running", "failed"}),
    "parent_running": frozenset({"after_snapshot_written", "failed"}),
    "after_snapshot_written": frozenset({"release_requested", "failed"}),
    "release_requested": frozenset({"released", "failed"}),
    "released": frozenset(),
    "failed": frozenset(),
}

@dataclass(frozen=True, slots=True)
class LeaseResult:
    lease: str
    generation: int
    request_sha256: str
    status: str
    parent_exit_code: int | None
    before_snapshot_sha256: str
    after_snapshot_sha256: str
    parent_reconcile_sha256: str
    durable_write_delta: tuple[str, ...]
    started_at: str
    ended_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.status not in {"succeeded", "failed"}:
            raise SyncStateError("Lease result status or schema is invalid.")
        for value in (
            self.request_sha256,
            self.before_snapshot_sha256,
            self.after_snapshot_sha256,
            self.parent_reconcile_sha256,
        ):
            require_digest(value, "lease result digest")
        if tuple(sorted(set(self.durable_write_delta))) != self.durable_write_delta:
            raise SyncStateError("Lease result durable write delta must be sorted and unique.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "after_snapshot_sha256": self.after_snapshot_sha256,
            "before_snapshot_sha256": self.before_snapshot_sha256,
            "durable_write_delta": list(self.durable_write_delta),
            "ended_at": self.ended_at,
            "generation": self.generation,
            "lease": self.lease,
            "parent_exit_code": self.parent_exit_code,
            "parent_reconcile_sha256": self.parent_reconcile_sha256,
            "request_sha256": self.request_sha256,
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "status": self.status,
        }

    def sha256(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> LeaseResult:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "lease result")
        delta = payload["durable_write_delta"]
        exit_code = payload["parent_exit_code"]
        if not isinstance(delta, list) or not all(isinstance(item, str) for item in delta):
            raise SyncStateError("Lease result durable write delta is malformed.")
        if exit_code is not None and type(exit_code) is not int:
            raise SyncStateError("Lease result parent exit code is malformed.")
        return cls(
            lease=require_string(payload, "lease"),
            generation=_int(payload, "generation"),
            request_sha256=require_string(payload, "request_sha256"),
            status=require_string(payload, "status"),
            parent_exit_code=exit_code,
            before_snapshot_sha256=require_string(payload, "before_snapshot_sha256"),
            after_snapshot_sha256=require_string(payload, "after_snapshot_sha256"),
            parent_reconcile_sha256=require_string(payload, "parent_reconcile_sha256"),
            durable_write_delta=tuple(cast(str, item) for item in delta),
            started_at=require_string(payload, "started_at"),
            ended_at=require_string(payload, "ended_at"),
            schema_version=_int(payload, "schema_version"),
        )


@dataclass(frozen=True, slots=True)
class LeaseRelease:
    lease: str
    generation: int
    expected_receipt_version: int
    expected_result_sha256: str
    requested_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.generation < 1 or self.expected_receipt_version < 1:
            raise SyncStateError("Lease release generation or schema is invalid.")
        require_digest(self.expected_result_sha256, "expected_result_sha256")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "expected_receipt_version": self.expected_receipt_version,
            "expected_result_sha256": self.expected_result_sha256,
            "generation": self.generation,
            "lease": self.lease,
            "requested_at": self.requested_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> LeaseRelease:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "lease release")
        return cls(
            lease=require_string(payload, "lease"),
            generation=_int(payload, "generation"),
            expected_receipt_version=_int(payload, "expected_receipt_version"),
            expected_result_sha256=require_string(payload, "expected_result_sha256"),
            requested_at=require_string(payload, "requested_at"),
            schema_version=_int(payload, "schema_version"),
        )


def advance_lease_receipt(
    receipt: LeaseReceipt,
    *,
    next_phase: str,
    expected_generation: int,
    expected_receipt_version: int,
    heartbeat_at: str | None = None,
    heartbeat_deadline_at: str | None = None,
    lock_inode: int | None = None,
    result_sha256: str | None = None,
    error: str | None = None,
) -> LeaseReceipt:
    if (receipt.generation, receipt.receipt_version) != (
        expected_generation,
        expected_receipt_version,
    ):
        raise SyncStateError("Lease receipt CAS mismatch.")
    if next_phase not in _TRANSITIONS[receipt.phase]:
        raise SyncStateError("Lease receipt transition is invalid.")
    return replace(
        receipt,
        generation=receipt.generation + 1,
        receipt_version=receipt.receipt_version + 1,
        phase=next_phase,
        heartbeat_at=heartbeat_at if heartbeat_at is not None else receipt.heartbeat_at,
        heartbeat_deadline_at=(
            heartbeat_deadline_at
            if heartbeat_deadline_at is not None
            else receipt.heartbeat_deadline_at
        ),
        lock_inode=lock_inode if lock_inode is not None else receipt.lock_inode,
        result_sha256=result_sha256 if result_sha256 is not None else receipt.result_sha256,
        error=error,
    )


def lease_is_fresh(receipt: LeaseReceipt, now: datetime) -> bool:
    if receipt.heartbeat_deadline_at is None:
        return False
    deadline = datetime.fromisoformat(receipt.heartbeat_deadline_at)
    if deadline.tzinfo is None or now.tzinfo is None:
        raise SyncStateError("Lease freshness requires timezone-aware timestamps.")
    return now <= deadline


def _int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise SyncStateError(f"{key} must be an integer.")
    return value
