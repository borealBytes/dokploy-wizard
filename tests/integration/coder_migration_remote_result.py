from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

_REQUIRED_KEYS: Final = frozenset(
    {
        "cause",
        "cleanup",
        "delete_transition",
        "phase",
        "receipt_generation",
        "receipt_status",
        "receipt_token_advanced",
        "schema_version",
        "skipped",
        "start_transition",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class RemoteCoderResultError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class RemoteCoderRuntimeResult:
    status: str
    phase: str
    cause: str
    skipped: int
    start_transition: bool
    delete_transition: bool
    receipt_status: str
    receipt_generation: int
    receipt_token_advanced: bool
    cleanup: str


def result_bytes(result: RemoteCoderRuntimeResult) -> bytes:
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "status": result.status,
        "phase": result.phase,
        "cause": result.cause,
        "skipped": result.skipped,
        "start_transition": result.start_transition,
        "delete_transition": result.delete_transition,
        "receipt_status": result.receipt_status,
        "receipt_generation": result.receipt_generation,
        "receipt_token_advanced": result.receipt_token_advanced,
        "cleanup": result.cleanup,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_runtime_result(payload: bytes) -> RemoteCoderRuntimeResult:
    mapping = _mapping(payload)
    if frozenset(mapping) != _REQUIRED_KEYS:
        raise RemoteCoderResultError("remote runtime result keys are not exact")
    match _nonnegative_int(mapping["schema_version"], "schema_version"):
        case 1:
            pass
        case _:
            raise RemoteCoderResultError("remote runtime result schema version is unsupported")
    result = RemoteCoderRuntimeResult(
        status=_text(mapping["status"], "status"),
        phase=_text(mapping["phase"], "phase"),
        cause=_text(mapping["cause"], "cause"),
        skipped=_nonnegative_int(mapping["skipped"], "skipped"),
        start_transition=_bool(mapping["start_transition"], "start_transition"),
        delete_transition=_bool(mapping["delete_transition"], "delete_transition"),
        receipt_status=_text(mapping["receipt_status"], "receipt_status"),
        receipt_generation=_nonnegative_int(mapping["receipt_generation"], "receipt_generation"),
        receipt_token_advanced=_bool(mapping["receipt_token_advanced"], "receipt_token_advanced"),
        cleanup=_text(mapping["cleanup"], "cleanup"),
    )
    if result.cleanup != "complete":
        raise RemoteCoderResultError("remote runtime cleanup is incomplete")
    return result


def _mapping(payload: bytes) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteCoderResultError("remote runtime result is not JSON") from error
    match value:
        case dict() as mapping:
            return mapping
        case _:
            raise RemoteCoderResultError("remote runtime result must be an object")


def _text(value: JsonValue, field: str) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise RemoteCoderResultError(f"remote runtime result {field} must be text")


def _nonnegative_int(value: JsonValue, field: str) -> int:
    match value:
        case bool():
            raise RemoteCoderResultError(f"remote runtime result {field} must be an integer")
        case int() as number if number >= 0:
            return number
        case _:
            raise RemoteCoderResultError(f"remote runtime result {field} must be an integer")


def _bool(value: JsonValue, field: str) -> bool:
    match value:
        case bool() as flag:
            return flag
        case _:
            raise RemoteCoderResultError(f"remote runtime result {field} must be a boolean")


__all__ = [
    "RemoteCoderResultError",
    "RemoteCoderRuntimeResult",
    "parse_runtime_result",
    "result_bytes",
]
