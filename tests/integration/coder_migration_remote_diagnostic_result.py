from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, TypeAlias

STAGES: Final = (
    "package-import",
    "module-import",
    "images",
    "stack-construction",
    "run",
    "serialization",
    "entrypoint",
)
_RESULT_KEYS: Final = frozenset(
    {
        "completed",
        "error_category",
        "error_origin",
        "error_type",
        "runtime_cause",
        "runtime_phase",
        "runtime_status",
        "stage",
    }
)
_SAFE_ERROR_TYPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*\Z")
_SAFE_ERROR_ORIGIN: Final = re.compile(r"[A-Za-z0-9_.-]+:[0-9]+:[A-Za-z0-9_]+\Z")
_ERROR_CATEGORIES: Final = frozenset(
    {
        "attribute",
        "import",
        "module-not-found",
        "none",
        "os",
        "runtime",
        "syntax",
        "system-exit",
        "type",
        "unexpected",
        "value",
    }
)
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class RemoteDiagnosticResult:
    stage: str
    completed: tuple[str, ...]
    error_category: str
    error_origin: str
    error_type: str
    runtime_status: str
    runtime_phase: str
    runtime_cause: str


@dataclass(frozen=True, slots=True)
class RemoteDiagnosticError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def result_bytes(result: RemoteDiagnosticResult) -> bytes:
    payload = {
        "completed": result.completed,
        "error_category": result.error_category,
        "error_origin": result.error_origin,
        "error_type": result.error_type,
        "runtime_cause": result.runtime_cause,
        "runtime_phase": result.runtime_phase,
        "runtime_status": result.runtime_status,
        "stage": result.stage,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_diagnostic_result(payload: bytes) -> RemoteDiagnosticResult:
    try:
        value: JsonValue = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteDiagnosticError("remote diagnostic is not JSON") from error
    match value:
        case dict() as mapping:
            pass
        case _:
            raise RemoteDiagnosticError("remote diagnostic must be an object")
    if frozenset(mapping) != _RESULT_KEYS:
        raise RemoteDiagnosticError("remote diagnostic keys are not exact")
    stage = _text(mapping["stage"], "stage")
    if stage not in STAGES:
        raise RemoteDiagnosticError("remote diagnostic stage is unknown")
    completed = _text_sequence(mapping["completed"])
    error_category = _text(mapping["error_category"], "error_category")
    if error_category not in _ERROR_CATEGORIES:
        raise RemoteDiagnosticError("remote diagnostic error category is unknown")
    error_origin = _text(mapping["error_origin"], "error_origin")
    error_type = _text(mapping["error_type"], "error_type")
    if error_category == "none":
        if error_origin != "none" or error_type != "none":
            raise RemoteDiagnosticError("remote diagnostic success error fields are invalid")
    elif (
        _SAFE_ERROR_ORIGIN.fullmatch(error_origin) is None
        or _SAFE_ERROR_TYPE.fullmatch(error_type) is None
    ):
        raise RemoteDiagnosticError("remote diagnostic failure location is invalid")
    expected_completed = STAGES if error_category == "none" else STAGES[: STAGES.index(stage)]
    if completed != expected_completed:
        raise RemoteDiagnosticError("remote diagnostic completed stages are invalid")
    if error_category == "none" and stage != "entrypoint":
        raise RemoteDiagnosticError("remote diagnostic success must reach the entrypoint")
    return RemoteDiagnosticResult(
        stage=stage,
        completed=completed,
        error_category=error_category,
        error_origin=error_origin,
        error_type=error_type,
        runtime_status=_text(mapping["runtime_status"], "runtime_status"),
        runtime_phase=_text(mapping["runtime_phase"], "runtime_phase"),
        runtime_cause=_text(mapping["runtime_cause"], "runtime_cause"),
    )


def _text_sequence(value: JsonValue) -> tuple[str, ...]:
    match value:
        case list() as values:
            return tuple(_text(item, "completed") for item in values)
        case _:
            raise RemoteDiagnosticError("remote diagnostic completed must be an array")


def _text(value: JsonValue, field: str) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise RemoteDiagnosticError(f"remote diagnostic {field} must be text")
