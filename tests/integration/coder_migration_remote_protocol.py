from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeAlias

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderWorkspace,
    JsonValue,
    parse_build,
    parse_coder_id,
    parse_workspace,
)


@dataclass(frozen=True, slots=True)
class RemoteCoderProtocolError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class RemoteCoderResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CoderBuildInfo:
    version: str


@dataclass(frozen=True, slots=True)
class CoderUser:
    id: CoderId


@dataclass(frozen=True, slots=True)
class CreatedFirstUser:
    user_id: CoderId
    organization_id: CoderId


@dataclass(frozen=True, slots=True)
class FirstUserMissing:
    build_version: str


@dataclass(frozen=True, slots=True)
class FirstUserPresent:
    user: CoderUser


FirstUserProbe: TypeAlias = FirstUserMissing | FirstUserPresent


@dataclass(frozen=True, slots=True)
class CoderLogin:
    session_token: str


@dataclass(frozen=True, slots=True)
class CoderOrganization:
    id: CoderId


def parse_healthz(response: RemoteCoderResponse) -> None:
    if response.status != 200 or response.body != b"OK":
        raise RemoteCoderProtocolError("Coder healthz must return HTTP 200 with OK")


def parse_build_info(response: RemoteCoderResponse) -> CoderBuildInfo:
    _require_status(response, 200, "Coder buildinfo")
    mapping = _json_object(response.body, "Coder buildinfo")
    return CoderBuildInfo(version=_required_text(mapping, "version", "Coder buildinfo"))


def parse_first_user_probe(response: RemoteCoderResponse) -> FirstUserProbe:
    match response.status:
        case 404:
            return FirstUserMissing(
                build_version=_required_header(response, "x-coder-build-version")
            )
        case 200:
            mapping = _json_object(response.body, "Coder first user")
            return FirstUserPresent(
                user=CoderUser(id=parse_coder_id(mapping.get("id"), "Coder first user.id"))
            )
        case _:
            raise RemoteCoderProtocolError("Coder first user must return HTTP 404 or 200")


def parse_created_user(response: RemoteCoderResponse) -> CreatedFirstUser:
    _require_status(response, 201, "Coder first user creation")
    mapping = _json_object(response.body, "Coder first user")
    return CreatedFirstUser(
        user_id=parse_coder_id(mapping.get("user_id"), "Coder first user.user_id"),
        organization_id=parse_coder_id(
            mapping.get("organization_id"), "Coder first user.organization_id"
        ),
    )


def parse_login(response: RemoteCoderResponse) -> CoderLogin:
    _require_status(response, 201, "Coder login")
    mapping = _json_object(response.body, "Coder login")
    return CoderLogin(session_token=_required_text(mapping, "session_token", "Coder login"))


def parse_default_organization(response: RemoteCoderResponse) -> CoderOrganization:
    _require_status(response, 200, "Coder default organization")
    mapping = _json_object(response.body, "Coder default organization")
    return CoderOrganization(
        id=parse_coder_id(mapping.get("id"), "Coder default organization.id")
    )


def parse_created_workspace(response: RemoteCoderResponse) -> CoderWorkspace:
    _require_status(response, 201, "Coder workspace creation")
    return parse_workspace(_json_value(response.body, "Coder workspace"))


def parse_created_build(response: RemoteCoderResponse, label: str) -> CoderBuild:
    _require_status(response, 201, label)
    return parse_build(_json_value(response.body, label))


def workspace_build_failure_category(response: RemoteCoderResponse) -> str:
    if response.status != 200:
        return "workspace-build-detail-http"
    mapping = _json_object(response.body, "Coder workspace build detail")
    match mapping.get("job"):
        case dict() as job:
            match job.get("error_code"):
                case str() as code if "terraform" in code.lower():
                    return "template-build"
                case str() as code if "provisioner" in code.lower():
                    return "provisioner-build"
                case str() as code if "agent" in code.lower():
                    return "agent-connectivity"
                case str() as code if "deadline" in code.lower():
                    return "build-deadline"
                case str() | None:
                    return "workspace-build-unknown"
                case _:
                    return "workspace-build-unknown"
        case _:
            return "workspace-build-unknown"


def _require_status(response: RemoteCoderResponse, expected: int, label: str) -> None:
    if response.status != expected:
        raise RemoteCoderProtocolError(f"{label} must return HTTP {expected}")


def _required_header(response: RemoteCoderResponse, name: str) -> str:
    for candidate, value in response.headers:
        if candidate == name and value:
            return value
    raise RemoteCoderProtocolError(f"Coder first-user 404 requires {name} header")


def _json_value(payload: bytes, label: str) -> JsonValue:
    try:
        value: JsonValue = json.loads(payload.decode("utf-8"))
        return value
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteCoderProtocolError(f"{label} must be JSON") from error


def _json_object(payload: bytes, label: str) -> dict[str, JsonValue]:
    value = _json_value(payload, label)
    match value:
        case dict() as mapping:
            return mapping
        case _:
            raise RemoteCoderProtocolError(f"{label} must be a JSON object")


def _required_text(mapping: dict[str, JsonValue], key: str, label: str) -> str:
    value = mapping.get(key)
    match value:
        case str() as text if text:
            return text
        case _:
            raise RemoteCoderProtocolError(f"{label}.{key} must be a non-empty string")


__all__ = [
    "CoderBuildInfo",
    "CoderLogin",
    "CoderOrganization",
    "CoderUser",
    "CreatedFirstUser",
    "FirstUserMissing",
    "FirstUserPresent",
    "FirstUserProbe",
    "RemoteCoderProtocolError",
    "RemoteCoderResponse",
    "parse_build_info",
    "parse_created_build",
    "parse_created_user",
    "parse_created_workspace",
    "workspace_build_failure_category",
    "parse_default_organization",
    "parse_first_user_probe",
    "parse_healthz",
    "parse_login",
]
