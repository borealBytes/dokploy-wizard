from __future__ import annotations

import json
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Final, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderBuildTransition,
    CoderProtocolError,
    CoderTemplate,
    CoderWorkspace,
    JsonValue,
    parse_build,
    parse_coder_error,
    parse_coder_id,
    parse_template,
    parse_workspace,
)

_PAGE_SIZE: Final = 100
_MAX_PAGES: Final = 1000
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CoderHttpRequest:
    method: str
    path: str
    body: bytes | None
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CoderHttpResponse:
    status: int
    body: bytes


class CoderTransportError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


class CoderTransport(Protocol):
    def send(self, request: CoderHttpRequest) -> CoderHttpResponse: ...


class UrllibCoderTransport:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def send(self, request: CoderHttpRequest) -> CoderHttpResponse:
        request_value = urlrequest.Request(
            f"{self._base_url}{request.path}",
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        opener = urlrequest.build_opener(_NoRedirect())
        try:
            with opener.open(request_value, timeout=30) as response:
                return CoderHttpResponse(status=response.status, body=_read_response(response))
        except urlerror.HTTPError as error:
            return CoderHttpResponse(status=error.code, body=_read_response(error))
        except urlerror.URLError as error:
            raise CoderTransportError("Coder transport failed") from error


class CoderMigrationApi:
    def __init__(self, *, transport: CoderTransport, session_token: str) -> None:
        if not session_token:
            raise CoderProtocolError("Coder session token must be non-empty")
        self._transport = transport
        self._session_token = session_token

    def default_organization_id(self) -> str:
        value = self._json("GET", "/api/v2/organizations/default", None, 200)
        mapping = _mapping(value, "default organization")
        return str(parse_coder_id(mapping.get("id"), "default organization.id"))

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        organization = parse_coder_id(organization_id, "organization_id")
        value = self._json(
            "GET", f'/api/v2/templates?q=organization%3A%22{organization}%22', None, 200
        )
        return tuple(parse_template(item) for item in _array(value, "templates"))

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        workspaces: list[CoderWorkspace] = []
        expected_count: int | None = None
        for page_index in range(_MAX_PAGES):
            offset = page_index * _PAGE_SIZE
            value = self._json(
                "GET", f"/api/v2/workspaces?q=&limit=100&offset={offset}", None, 200
            )
            mapping = _mapping(value, "workspace page")
            count = _count(mapping.get("count"))
            records = tuple(
                parse_workspace(item) for item in _array(mapping.get("workspaces"), "workspaces")
            )
            if expected_count is None:
                expected_count = count
            elif expected_count != count:
                raise CoderProtocolError("workspace count changed during pagination")
            if len(workspaces) + len(records) > count:
                raise CoderProtocolError("workspace page exceeds declared count")
            workspaces.extend(records)
            if len(workspaces) == count:
                _unique_workspace_ids(workspaces)
                return tuple(workspaces)
            if not records:
                raise CoderProtocolError("workspace page ended before declared count")
        raise CoderProtocolError("workspace pagination exceeded its bound")

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        workspace = parse_coder_id(workspace_id, "workspace_id")
        builds: list[CoderBuild] = []
        for page_index in range(_MAX_PAGES):
            offset = page_index * _PAGE_SIZE
            value = self._json(
                "GET", f"/api/v2/workspaces/{workspace}/builds?limit=100&offset={offset}", None, 200
            )
            records = tuple(parse_build(item) for item in _array(value, "workspace builds"))
            builds.extend(records)
            if len(records) < _PAGE_SIZE:
                _unique_builds(builds)
                return tuple(builds)
        raise CoderProtocolError("workspace build pagination exceeded its bound")

    def rename_template(self, template_id: str, name: str) -> CoderTemplate:
        template = parse_coder_id(template_id, "template_id")
        value = self._json("PATCH", f"/api/v2/templates/{template}", {"name": name}, 200)
        return parse_template(value)

    def create_workspace(self, *, template_id: str, workspace_name: str) -> CoderWorkspace:
        template = parse_coder_id(template_id, "template_id")
        value = self._json(
            "POST",
            "/api/v2/users/me/workspaces",
            {"name": workspace_name, "template_id": str(template)},
            201,
        )
        return parse_workspace(value)

    def submit_workspace_transition(
        self,
        workspace_id: str,
        transition: CoderBuildTransition,
    ) -> CoderBuild:
        workspace = parse_coder_id(workspace_id, "workspace_id")
        value = self._json(
            "POST",
            f"/api/v2/workspaces/{workspace}/builds",
            {"transition": transition},
            201,
        )
        return parse_build(value)

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        workspace = parse_coder_id(workspace_id, "workspace_id")
        value = self._json(
            "POST",
            f"/api/v2/workspaces/{workspace}/builds",
            {"transition": "delete", "orphan": False},
            201,
        )
        return parse_build(value)

    def delete_template(self, template_id: str) -> bytes:
        template = parse_coder_id(template_id, "template_id")
        return self._raw("DELETE", f"/api/v2/templates/{template}", None, 200).body

    def _json(
        self, method: str, path: str, payload: dict[str, JsonValue] | None, expected_status: int
    ) -> JsonValue:
        response = self._raw(method, path, payload, expected_status)
        try:
            value: JsonValue = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoderProtocolError("Coder response is not JSON") from error
        return value

    def _raw(
        self, method: str, path: str, payload: dict[str, JsonValue] | None, expected_status: int
    ) -> CoderHttpResponse:
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        request = CoderHttpRequest(
            method=method,
            path=path,
            body=body,
            headers=(
                ("Accept", "application/json"),
                ("Content-Type", "application/json"),
                ("Coder-Session-Token", self._session_token),
            ),
        )
        response = self._transport.send(request)
        if response.status == expected_status:
            return response
        try:
            error_value: JsonValue = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoderProtocolError("Coder error response is not JSON") from error
        raise parse_coder_error(response.status, error_value)


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urlrequest.Request,
        _response: IO[bytes],
        _code: int,
        _message: str,
        _headers: HTTPMessage,
        _target: str,
    ) -> None:
        return None


def _read_response(response: IO[bytes]) -> bytes:
    payload = bytearray()
    while len(payload) <= _MAX_RESPONSE_BYTES:
        chunk = response.read(min(64 * 1024, _MAX_RESPONSE_BYTES + 1 - len(payload)))
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
    raise CoderTransportError("Coder response exceeds the bounded transport limit")


def _mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    match value:
        case dict() as mapping:
            return mapping
        case _:
            raise CoderProtocolError(f"{label} must be an object")


def _array(value: JsonValue | None, label: str) -> list[JsonValue]:
    match value:
        case list() as values:
            return values
        case _:
            raise CoderProtocolError(f"{label} must be an array")


def _count(value: JsonValue | None) -> int:
    match value:
        case bool():
            raise CoderProtocolError("workspace count must be a non-negative integer")
        case int() as count if count >= 0:
            return count
        case _:
            raise CoderProtocolError("workspace count must be a non-negative integer")


def _unique_workspace_ids(workspaces: list[CoderWorkspace]) -> None:
    ids = tuple(workspace.id for workspace in workspaces)
    if len(set(ids)) != len(ids):
        raise CoderProtocolError("workspace pagination contains duplicate IDs")


def _unique_builds(builds: list[CoderBuild]) -> None:
    ids = tuple(build.id for build in builds)
    numbers = tuple(build.build_number for build in builds)
    if len(set(ids)) != len(ids) or len(set(numbers)) != len(numbers):
        raise CoderProtocolError("workspace build pagination contains duplicate identities")
