from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, NewType, TypeAlias
from uuid import UUID

CoderId = NewType("CoderId", str)
CoderBuildTransition: TypeAlias = Literal["start", "stop", "delete"]
CoderBuildStatus: TypeAlias = Literal[
    "pending",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
    "canceling",
    "canceled",
    "deleting",
    "deleted",
]
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class CoderValidation:
    field: str
    detail: str


class CoderApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        detail: str | None,
        validations: tuple[CoderValidation, ...],
    ) -> None:
        super().__init__(status, message, detail, validations)
        self.status = status
        self.message = message
        self.detail = detail
        self.validations = validations

    def __str__(self) -> str:
        return f"Coder API HTTP {self.status}: {self.message}"


class CoderProtocolError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderTemplate:
    id: CoderId
    organization_id: CoderId
    name: str


@dataclass(frozen=True, slots=True)
class CoderBuild:
    id: CoderId
    build_number: int
    transition: CoderBuildTransition
    status: CoderBuildStatus
    created_at: str


@dataclass(frozen=True, slots=True)
class CoderWorkspace:
    id: CoderId
    template_id: CoderId
    name: str
    latest_build: CoderBuild


def parse_coder_id(value: JsonValue, field: str) -> CoderId:
    match value:
        case str() as text:
            try:
                parsed = UUID(text)
            except ValueError as error:
                raise CoderProtocolError(f"{field} must be a UUID") from error
            if str(parsed) != text:
                raise CoderProtocolError(f"{field} must use canonical lowercase UUID form")
            return CoderId(text)
        case _:
            raise CoderProtocolError(f"{field} must be a UUID")


def parse_template(value: JsonValue) -> CoderTemplate:
    mapping = _mapping(value, "template")
    return CoderTemplate(
        id=parse_coder_id(mapping.get("id"), "template.id"),
        organization_id=parse_coder_id(mapping.get("organization_id"), "template.organization_id"),
        name=_text(mapping.get("name"), "template.name"),
    )


def parse_build(value: JsonValue) -> CoderBuild:
    mapping = _mapping(value, "build")
    return CoderBuild(
        id=parse_coder_id(mapping.get("id"), "build.id"),
        build_number=_nonnegative_int(mapping.get("build_number"), "build.build_number"),
        transition=_transition(mapping.get("transition")),
        status=_status(mapping.get("status")),
        created_at=_timestamp(mapping.get("created_at"), "build.created_at"),
    )


def parse_workspace(value: JsonValue) -> CoderWorkspace:
    mapping = _mapping(value, "workspace")
    return CoderWorkspace(
        id=parse_coder_id(mapping.get("id"), "workspace.id"),
        template_id=parse_coder_id(mapping.get("template_id"), "workspace.template_id"),
        name=_text(mapping.get("name"), "workspace.name"),
        latest_build=parse_build(mapping.get("latest_build")),
    )


def parse_coder_error(status: int, value: JsonValue) -> CoderApiError:
    mapping = _mapping(value, "Coder error")
    if "validations" not in mapping:
        validations: list[JsonValue] = []
    else:
        validation_value = mapping["validations"]
        if validation_value is None:
            raise CoderProtocolError("Coder error.validations is null")
        validations = _list(validation_value, "Coder error.validations")
    return CoderApiError(
        status=status,
        message=_text(mapping.get("message"), "Coder error.message"),
        detail=_nullable_text(mapping.get("detail"), "Coder error.detail"),
        validations=tuple(_validation(item) for item in validations),
    )


def _mapping(value: JsonValue | None, label: str) -> dict[str, JsonValue]:
    match value:
        case dict() as mapping:
            return mapping
        case _:
            raise CoderProtocolError(f"{label} must be an object")


def _list(value: JsonValue | None, label: str) -> list[JsonValue]:
    match value:
        case list() as values:
            return values
        case _:
            raise CoderProtocolError(f"{label} must be an array")


def _text(value: JsonValue | None, label: str) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise CoderProtocolError(f"{label} must be a non-empty string")


def _nullable_text(value: JsonValue | None, label: str) -> str | None:
    match value:
        case None:
            return None
        case str() as text:
            return text
        case _:
            raise CoderProtocolError(f"{label} must be a string or null")


def _nonnegative_int(value: JsonValue | None, label: str) -> int:
    match value:
        case bool():
            raise CoderProtocolError(f"{label} must be a non-negative integer")
        case int() as number if number >= 0:
            return number
        case _:
            raise CoderProtocolError(f"{label} must be a non-negative integer")


def _timestamp(value: JsonValue | None, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoderProtocolError(f"{label} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise CoderProtocolError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _transition(value: JsonValue | None) -> CoderBuildTransition:
    match value:
        case "start":
            return "start"
        case "stop":
            return "stop"
        case "delete":
            return "delete"
        case _:
            raise CoderProtocolError("build.transition is unknown")


def _status(value: JsonValue | None) -> CoderBuildStatus:
    match value:
        case "pending":
            return "pending"
        case "starting":
            return "starting"
        case "running":
            return "running"
        case "stopping":
            return "stopping"
        case "stopped":
            return "stopped"
        case "failed":
            return "failed"
        case "canceling":
            return "canceling"
        case "canceled":
            return "canceled"
        case "deleting":
            return "deleting"
        case "deleted":
            return "deleted"
        case _:
            raise CoderProtocolError("build.status is unknown")


def _validation(value: JsonValue) -> CoderValidation:
    mapping = _mapping(value, "Coder error validation")
    return CoderValidation(
        field=_text(mapping.get("field"), "Coder error validation.field"),
        detail=_text(mapping.get("detail"), "Coder error validation.detail"),
    )
