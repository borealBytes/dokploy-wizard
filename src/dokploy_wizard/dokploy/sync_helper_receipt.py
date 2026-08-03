"""Strict lease receipt schema for the external sync helper."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    require_digest,
    require_exact_keys,
    require_string,
)

LEASE_PHASES = frozenset(
    {
        "created",
        "starting",
        "acquired_held",
        "parent_running",
        "after_snapshot_written",
        "release_requested",
        "released",
        "failed",
    }
)


@dataclass(frozen=True, slots=True)
class LeaseReceipt:
    lease: str
    generation: int
    receipt_version: int
    phase: str
    container_id: str | None
    mode: str
    parent_pid: int
    parent_start_time_ticks: int
    parent_argv_sha256: str
    heartbeat_at: str | None
    heartbeat_deadline_at: str | None
    lock_inode: int | None
    request_sha256: str
    result_sha256: str | None
    tombstone_sha256: str | None
    error: str | None
    schema_version: int = 1

    @classmethod
    def created(cls, *, request: LeaseRequest, container_id: str | None) -> LeaseReceipt:
        return cls(
            lease=request.lease,
            generation=request.generation,
            receipt_version=request.receipt_version,
            phase="created",
            container_id=container_id,
            mode=request.mode,
            parent_pid=request.parent_pid,
            parent_start_time_ticks=request.parent_start_time_ticks,
            parent_argv_sha256=request.parent_argv_sha256,
            heartbeat_at=None,
            heartbeat_deadline_at=None,
            lock_inode=None,
            request_sha256=request.sha256(),
            result_sha256=None,
            tombstone_sha256=request.tombstone_sha256,
            error=None,
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.phase not in LEASE_PHASES:
            raise SyncStateError("Lease receipt phase or schema is invalid.")
        if self.lease == "" or self.mode not in {"reconcile", "disable"}:
            raise SyncStateError("Lease receipt identity is invalid.")
        if self.generation < 1 or self.receipt_version < 1:
            raise SyncStateError("Lease receipt generation is invalid.")
        if self.parent_pid < 1 or self.parent_start_time_ticks < 0:
            raise SyncStateError("Lease receipt parent identity is invalid.")
        if self.lock_inode is not None and self.lock_inode < 0:
            raise SyncStateError("Lease receipt lock inode is invalid.")
        require_digest(self.parent_argv_sha256, "parent_argv_sha256")
        require_digest(self.request_sha256, "request_sha256")
        for value in (self.result_sha256, self.tombstone_sha256):
            if value is not None:
                require_digest(value, "lease receipt digest")
        for value in (
            self.container_id,
            self.heartbeat_at,
            self.heartbeat_deadline_at,
            self.error,
        ):
            if value == "":
                raise SyncStateError("Lease receipt optional strings cannot be empty.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_id": self.container_id,
            "error": self.error,
            "generation": self.generation,
            "heartbeat_at": self.heartbeat_at,
            "heartbeat_deadline_at": self.heartbeat_deadline_at,
            "lease": self.lease,
            "lock_inode": self.lock_inode,
            "mode": self.mode,
            "parent_argv_sha256": self.parent_argv_sha256,
            "parent_pid": self.parent_pid,
            "parent_start_time_ticks": self.parent_start_time_ticks,
            "phase": self.phase,
            "receipt_version": self.receipt_version,
            "request_sha256": self.request_sha256,
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "tombstone_sha256": self.tombstone_sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> LeaseReceipt:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "lease receipt")
        return cls(
            lease=require_string(payload, "lease"),
            generation=_int(payload, "generation"),
            receipt_version=_int(payload, "receipt_version"),
            phase=require_string(payload, "phase"),
            container_id=_optional_string(payload, "container_id"),
            mode=require_string(payload, "mode"),
            parent_pid=_int(payload, "parent_pid"),
            parent_start_time_ticks=_int(payload, "parent_start_time_ticks"),
            parent_argv_sha256=require_string(payload, "parent_argv_sha256"),
            heartbeat_at=_optional_string(payload, "heartbeat_at"),
            heartbeat_deadline_at=_optional_string(payload, "heartbeat_deadline_at"),
            lock_inode=_optional_int(payload, "lock_inode"),
            request_sha256=require_string(payload, "request_sha256"),
            result_sha256=_optional_string(payload, "result_sha256"),
            tombstone_sha256=_optional_string(payload, "tombstone_sha256"),
            error=_optional_string(payload, "error"),
            schema_version=_int(payload, "schema_version"),
        )


def _int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise SyncStateError(f"{key} must be an integer.")
    return value


def _optional_int(payload: dict[str, JsonValue], key: str) -> int | None:
    value = payload[key]
    if value is not None and type(value) is not int:
        raise SyncStateError(f"{key} must be an integer or null.")
    return value


def _optional_string(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload[key]
    if value is not None and (not isinstance(value, str) or value == ""):
        raise SyncStateError(f"{key} must be a non-empty string or null.")
    return value
