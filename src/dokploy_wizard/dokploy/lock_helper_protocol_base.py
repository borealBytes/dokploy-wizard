"""Primitive models and validators for copied lock-helper IPC."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TypeAlias

JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class ProtocolError(RuntimeError):
    """Raised when helper IPC bytes violate their exact schema or binding."""


@dataclass(frozen=True, slots=True)
class Request:
    payload: dict[str, JsonValue]
    lease: str
    generation: int
    receipt_version: int
    mode: str
    parent_pid: int
    parent_start_time_ticks: int
    parent_argv_sha256: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Receipt:
    payload: dict[str, JsonValue]
    lease: str
    generation: int
    receipt_version: int
    phase: str
    container_id: str
    request_sha256: str


@dataclass(frozen=True, slots=True)
class Result:
    payload: dict[str, JsonValue]
    lease: str
    generation: int
    request_sha256: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Release:
    lease: str
    generation: int
    expected_receipt_version: int
    expected_result_sha256: str


def canonical_sha256(payload: dict[str, JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def exact(payload: dict[str, JsonValue], keys: frozenset[str], name: str) -> None:
    if set(payload) != keys:
        raise ProtocolError(f"{name} keys are invalid")


def schema(payload: dict[str, JsonValue]) -> None:
    if payload["schema_version"] != 1:
        raise ProtocolError("schema version is invalid")


def text(payload: dict[str, JsonValue], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or value == "":
        raise ProtocolError(f"{key} is invalid")
    return value


def positive_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value < 1:
        raise ProtocolError(f"{key} is invalid")
    return value


def nonnegative_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value < 0:
        raise ProtocolError(f"{key} is invalid")
    return value


def digest(payload: dict[str, JsonValue], key: str) -> str:
    value = text(payload, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProtocolError(f"{key} is invalid")
    return value


def optional_digest(payload: dict[str, JsonValue], key: str) -> str | None:
    if payload[key] is None:
        return None
    return digest(payload, key)
