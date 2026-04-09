"""Minimal Dokploy API client for compose-backed shared-core deployment."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import error, request

RequestFn = Callable[[request.Request], Any]


class DokployApiError(RuntimeError):
    """Raised when Dokploy API requests fail."""


@dataclass(frozen=True)
class DokployComposeSummary:
    compose_id: str
    name: str
    status: str | None


@dataclass(frozen=True)
class DokployEnvironmentSummary:
    environment_id: str
    name: str
    is_default: bool
    composes: tuple[DokployComposeSummary, ...]


@dataclass(frozen=True)
class DokployProjectSummary:
    project_id: str
    name: str
    environments: tuple[DokployEnvironmentSummary, ...]


@dataclass(frozen=True)
class DokployCreatedProject:
    project_id: str
    environment_id: str


@dataclass(frozen=True)
class DokployComposeRecord:
    compose_id: str
    name: str


@dataclass(frozen=True)
class DokployDeployResult:
    success: bool
    compose_id: str
    message: str | None


class DokployApiClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        request_fn: RequestFn | None = None,
    ) -> None:
        self._api_url = api_url.removesuffix("/").removesuffix("/api")
        self._api_key = api_key
        self._request_fn = request_fn or _default_request

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        payload = self._request_json("GET", "/api/project.all")
        if not isinstance(payload, list):
            raise DokployApiError("Dokploy project.all response must be a list.")
        return tuple(_parse_project_summary(item) for item in payload)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        payload = self._request_json(
            "POST",
            "/api/project.create",
            {
                "name": name,
                "description": description,
                "env": env or "",
            },
        )
        if not isinstance(payload, dict):
            raise DokployApiError("Dokploy project.create response must be an object.")
        project = payload.get("project")
        environment = payload.get("environment")
        if not isinstance(project, dict) or not isinstance(environment, dict):
            raise DokployApiError(
                "Dokploy project.create response must contain project and environment objects."
            )
        project_id = _require_string(project, "projectId")
        environment_id = _require_string(environment, "environmentId")
        return DokployCreatedProject(project_id=project_id, environment_id=environment_id)

    def create_compose(
        self,
        *,
        name: str,
        environment_id: str,
        compose_file: str,
        app_name: str,
    ) -> DokployComposeRecord:
        payload = self._request_json(
            "POST",
            "/api/compose.create",
            {
                "name": name,
                "environmentId": environment_id,
                "composeType": "docker-compose",
                "appName": app_name,
                "composeFile": compose_file,
            },
        )
        return _parse_compose_record(payload, "compose.create")

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        payload = self._request_json(
            "POST",
            "/api/compose.update",
            {
                "composeId": compose_id,
                "composeType": "docker-compose",
                "sourceType": "raw",
                "composeFile": compose_file,
            },
        )
        return _parse_compose_record(payload, "compose.update")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        payload = self._request_json(
            "POST",
            "/api/compose.deploy",
            {
                "composeId": compose_id,
                "title": title,
                "description": description,
            },
        )
        if payload is True:
            return DokployDeployResult(success=True, compose_id=compose_id, message=None)
        if not isinstance(payload, dict):
            raise DokployApiError("Dokploy compose.deploy response must be true or an object.")
        success = payload.get("success")
        message = payload.get("message")
        returned_compose_id = payload.get("composeId", compose_id)
        if not isinstance(success, bool):
            raise DokployApiError("Dokploy compose.deploy response must include boolean success.")
        if message is not None and not isinstance(message, str):
            raise DokployApiError("Dokploy compose.deploy response message must be a string.")
        if not isinstance(returned_compose_id, str):
            raise DokployApiError("Dokploy compose.deploy response composeId must be a string.")
        return DokployDeployResult(
            success=success,
            compose_id=returned_compose_id,
            message=message,
        )

    def _request_json(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "x-api-key": self._api_key,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            url=f"{self._api_url}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            response = self._request_fn(req)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise DokployApiError(
                f"Dokploy API request failed with status {exc.code}: {body or exc.reason}."
            ) from exc
        except error.URLError as exc:
            raise DokployApiError(f"Dokploy API request failed: {exc.reason}.") from exc
        if isinstance(response, list):
            return response
        if not isinstance(response, dict):
            raise DokployApiError("Dokploy API response must decode to a JSON object or array.")
        return response.get("data", response)


def _default_request(req: request.Request) -> Any:
    with request.urlopen(req, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _parse_project_summary(payload: Any) -> DokployProjectSummary:
    if not isinstance(payload, dict):
        raise DokployApiError("Dokploy project summary must be an object.")
    environments_payload = payload.get("environments")
    if not isinstance(environments_payload, list):
        raise DokployApiError("Dokploy project summary environments must be a list.")
    return DokployProjectSummary(
        project_id=_require_string(payload, "projectId"),
        name=_require_string(payload, "name"),
        environments=tuple(_parse_environment_summary(item) for item in environments_payload),
    )


def _parse_environment_summary(payload: Any) -> DokployEnvironmentSummary:
    if not isinstance(payload, dict):
        raise DokployApiError("Dokploy environment summary must be an object.")
    compose_payload = payload.get("compose")
    if not isinstance(compose_payload, list):
        raise DokployApiError("Dokploy environment compose list must be a list.")
    is_default = payload.get("isDefault")
    if not isinstance(is_default, bool):
        raise DokployApiError("Dokploy environment isDefault must be a boolean.")
    return DokployEnvironmentSummary(
        environment_id=_require_string(payload, "environmentId"),
        name=_require_string(payload, "name"),
        is_default=is_default,
        composes=tuple(_parse_compose_summary(item) for item in compose_payload),
    )


def _parse_compose_summary(payload: Any) -> DokployComposeSummary:
    if not isinstance(payload, dict):
        raise DokployApiError("Dokploy compose summary must be an object.")
    status = payload.get("composeStatus")
    if status is not None and not isinstance(status, str):
        raise DokployApiError("Dokploy compose status must be a string or null.")
    return DokployComposeSummary(
        compose_id=_require_string(payload, "composeId"),
        name=_require_string(payload, "name"),
        status=status,
    )


def _parse_compose_record(payload: Any, operation: str) -> DokployComposeRecord:
    if not isinstance(payload, dict):
        raise DokployApiError(f"Dokploy {operation} response must be an object.")
    return DokployComposeRecord(
        compose_id=_require_string(payload, "composeId"),
        name=_require_string(payload, "name"),
    )


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise DokployApiError(f"Dokploy API field '{key}' must be a non-empty string.")
    return value
