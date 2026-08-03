"""Value and phase invariants for copied lock-helper IPC documents."""

from __future__ import annotations

from datetime import datetime
from typing import TypeAlias

JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

_NONFAILED_OFFSETS = {
    "created": 1,
    "starting": 2,
    "acquired_held": 3,
    "parent_running": 4,
    "after_snapshot_written": 5,
    "release_requested": 6,
    "released": 7,
}
_FAILED_FIELDS = {
    2: (False, False),
    3: (False, False),
    4: (True, False),
    5: (True, False),
    6: (True, True),
    7: (True, True),
}


class ContractValueError(RuntimeError):
    """Raised when an exact copied IPC value is invalid."""


def timestamp(value: JsonValue, field: str) -> datetime:
    if not isinstance(value, str) or value == "":
        raise ContractValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractValueError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValueError(f"{field} is invalid")
    return parsed


def optional_timestamp(value: JsonValue, field: str) -> datetime | None:
    if value is None:
        return None
    return timestamp(value, field)


def optional_text(value: JsonValue, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ContractValueError(f"{field} is invalid")
    return value


def optional_positive_int(value: JsonValue, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ContractValueError(f"{field} is invalid")
    return value


def validate_receipt_phase(
    *,
    phase: str,
    generation_offset: int,
    version_offset: int,
    heartbeat_at: datetime | None,
    heartbeat_deadline_at: datetime | None,
    lock_inode: int | None,
    result_sha256: str | None,
    error: str | None,
) -> None:
    if generation_offset != version_offset:
        raise ContractValueError("receipt generation/version relationship is invalid")
    if phase == "failed":
        expected_fields = _FAILED_FIELDS.get(generation_offset)
        if expected_fields is None or error is None:
            raise ContractValueError("failed receipt invariants are invalid")
        expects_lock, expects_result = expected_fields
        if (lock_inode is not None) != expects_lock or (
            result_sha256 is not None
        ) != expects_result:
            raise ContractValueError("failed receipt invariants are invalid")
    else:
        if generation_offset != _NONFAILED_OFFSETS[phase] or error is not None:
            raise ContractValueError(f"{phase} receipt invariants are invalid")
    if phase == "created":
        control_values = (
            heartbeat_at,
            heartbeat_deadline_at,
            lock_inode,
            result_sha256,
        )
        if any(value is not None for value in control_values):
            raise ContractValueError("created receipt invariants are invalid")
        return
    if (
        heartbeat_at is None
        or heartbeat_deadline_at is None
        or heartbeat_at > heartbeat_deadline_at
    ):
        raise ContractValueError(f"{phase} receipt heartbeat invariants are invalid")
    if phase == "starting" and (lock_inode is not None or result_sha256 is not None):
        raise ContractValueError("starting receipt invariants are invalid")
    if phase in {"acquired_held", "parent_running"} and (
        lock_inode is None or result_sha256 is not None
    ):
        raise ContractValueError(f"{phase} receipt invariants are invalid")
    if phase in {"after_snapshot_written", "release_requested", "released"} and (
        lock_inode is None or result_sha256 is None
    ):
        raise ContractValueError(f"{phase} receipt invariants are invalid")
