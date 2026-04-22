"""Headless local Dokploy auth bootstrap for first-run installs."""

from __future__ import annotations

import http.cookiejar
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import error, request

AUTH_SIGN_IN_PATHS = ("/api/auth/sign-in/email", "/api/auth/sign-in")
AUTH_SIGN_UP_PATHS = ("/api/auth/sign-up/email", "/api/auth/sign-up")
AUTH_SESSION_PATHS = ("/api/user.session", "/api/auth/get-session")
API_KEY_CREATE_PATH = "/api/user.createApiKey"
_RATE_LIMIT_RETRYABLE_PATHS = {*AUTH_SIGN_IN_PATHS, *AUTH_SIGN_UP_PATHS}
_RATE_LIMIT_RETRY_ATTEMPTS = 4
_RATE_LIMIT_RETRY_DELAY_SECONDS = 5.0

RequestFn = Callable[[request.Request, http.cookiejar.CookieJar], Any]


class DokployBootstrapAuthError(RuntimeError):
    """Raised when local Dokploy auth bootstrap fails."""


@dataclass(frozen=True)
class DokployBootstrapAuthResult:
    api_key: str
    api_url: str
    admin_email: str
    organization_id: str
    used_sign_up: bool
    auth_path: str
    session_path: str


class DokployBootstrapAuthClient:
    def __init__(self, *, base_url: str, request_fn: RequestFn | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookiejar = http.cookiejar.CookieJar()
        self._request_fn = request_fn or _default_request
        self._authenticated = False
        self._resolved_session: tuple[dict[str, Any], str] | None = None

    def ensure_api_key(
        self,
        *,
        admin_email: str,
        admin_password: str,
        key_name: str = "dokploy-wizard",
    ) -> DokployBootstrapAuthResult:
        auth_path, used_sign_up = self._authenticate(
            admin_email=admin_email,
            admin_password=admin_password,
        )
        session_payload, session_path = self._resolve_session()
        organization_id = _extract_active_organization_id(session_payload)
        api_key_payload = self._request_json(
            "POST",
            API_KEY_CREATE_PATH,
            {
                "name": key_name,
                "metadata": {"organizationId": organization_id},
            },
        )
        api_key = _extract_api_key(api_key_payload)
        return DokployBootstrapAuthResult(
            api_key=api_key,
            api_url=self._base_url,
            admin_email=admin_email,
            organization_id=organization_id,
            used_sign_up=used_sign_up,
            auth_path=auth_path,
            session_path=session_path,
        )

    def assign_domain_server(
        self,
        *,
        admin_email: str,
        admin_password: str,
        host: str,
        certificate_type: str,
        lets_encrypt_email: str,
        https: bool,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/trpc/settings.assignDomainServer?batch=1",
            {
                "0": {
                    "json": {
                        "host": host,
                        "certificateType": certificate_type,
                        "letsEncryptEmail": lets_encrypt_email,
                        "https": https,
                    }
                }
            },
        )
        if not isinstance(payload, list) or not payload:
            raise DokployBootstrapAuthError(
                "Dokploy settings.assignDomainServer response must decode to a "
                "non-empty JSON array."
            )
        first = payload[0]
        if not isinstance(first, dict):
            raise DokployBootstrapAuthError(
                "Dokploy settings.assignDomainServer batch item must decode to a JSON object."
            )
        result = first.get("result")
        if not isinstance(result, dict):
            raise DokployBootstrapAuthError(
                "Dokploy settings.assignDomainServer result must decode to a JSON object."
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise DokployBootstrapAuthError(
                "Dokploy settings.assignDomainServer data must decode to a JSON object."
            )
        json_payload = data.get("json")
        if not isinstance(json_payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy settings.assignDomainServer JSON payload must decode to a JSON object."
            )
        return json_payload

    def deploy_compose(
        self,
        *,
        admin_email: str,
        admin_password: str,
        compose_id: str,
        title: str | None,
        description: str | None,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/compose.deploy",
            {
                "composeId": compose_id,
                "title": title,
                "description": description,
            },
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session compose.deploy response must decode to a JSON object."
            )
        return payload

    def list_projects(
        self,
        *,
        admin_email: str,
        admin_password: str,
    ) -> list[dict[str, Any]]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json("GET", "/api/project.all", None)
        if not isinstance(payload, list):
            raise DokployBootstrapAuthError(
                "Dokploy session project.all response must decode to a JSON array."
            )
        return payload

    def create_project(
        self,
        *,
        admin_email: str,
        admin_password: str,
        name: str,
        description: str | None,
        env: str | None,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
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
            raise DokployBootstrapAuthError(
                "Dokploy session project.create response must decode to a JSON object."
            )
        return payload

    def delete_project(
        self,
        *,
        admin_email: str,
        admin_password: str,
        project_id: str,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/project.remove",
            {"projectId": project_id},
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session project.remove response must decode to a JSON object."
            )
        return payload

    def create_compose(
        self,
        *,
        admin_email: str,
        admin_password: str,
        name: str,
        environment_id: str,
        compose_file: str,
        app_name: str,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/compose.create",
            {
                "name": name,
                "environmentId": environment_id,
                "composeType": "docker-compose",
                "appName": app_name,
            },
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session compose.create response must decode to a JSON object."
            )
        compose_id = payload.get("composeId")
        if not isinstance(compose_id, str) or compose_id == "":
            raise DokployBootstrapAuthError(
                "Dokploy session compose.create response must include a valid composeId."
            )
        return self.update_compose(
            admin_email=admin_email,
            admin_password=admin_password,
            compose_id=compose_id,
            compose_file=compose_file,
        )

    def update_compose(
        self,
        *,
        admin_email: str,
        admin_password: str,
        compose_id: str,
        compose_file: str,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/compose.update",
            {
                "composeId": compose_id,
                "composeType": "docker-compose",
                "sourceType": "raw",
                "composePath": "./docker-compose.yml",
                "githubId": None,
                "repository": None,
                "owner": None,
                "branch": None,
                "composeFile": compose_file,
            },
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session compose.update response must decode to a JSON object."
            )
        return payload

    def _authenticate(self, *, admin_email: str, admin_password: str) -> tuple[str, bool]:
        if self._authenticated:
            return "cached-session", False
        first_auth_error: DokployBootstrapAuthError | None = None
        for path in AUTH_SIGN_IN_PATHS:
            try:
                self._request_json(
                    "POST",
                    path,
                    {"email": admin_email, "password": admin_password},
                )
                self._authenticated = True
                return path, False
            except DokployBootstrapAuthError as error_value:
                if str(error_value).startswith("endpoint-unavailable:"):
                    continue
                first_auth_error = error_value
                break

        for path in AUTH_SIGN_UP_PATHS:
            try:
                self._request_json(
                    "POST",
                    path,
                    {
                        "email": admin_email,
                        "password": admin_password,
                        "name": admin_email.split("@", 1)[0],
                    },
                )
                self._authenticated = True
                return path, True
            except DokployBootstrapAuthError as error_value:
                if str(error_value).startswith("endpoint-unavailable:"):
                    continue
                if first_auth_error is not None:
                    raise first_auth_error
                raise
        if first_auth_error is not None:
            raise first_auth_error
        raise DokployBootstrapAuthError(
            "Could not find a working Dokploy auth endpoint for email sign-in or sign-up."
        )

    def _resolve_session(self) -> tuple[dict[str, Any], str]:
        if self._resolved_session is not None:
            return self._resolved_session
        for path in AUTH_SESSION_PATHS:
            try:
                payload = self._request_json("GET", path, None)
                if not isinstance(payload, dict):
                    raise DokployBootstrapAuthError(
                        f"Dokploy auth response from {path} must decode to a JSON object."
                    )
                self._resolved_session = (payload, path)
                return self._resolved_session
            except DokployBootstrapAuthError as error_value:
                if str(error_value).startswith("endpoint-unavailable:"):
                    continue
                raise
        raise DokployBootstrapAuthError(
            "Could not find a working Dokploy session endpoint after authentication."
        )

    def _request_json(self, method: str, path: str, payload: Any | None) -> Any:
        attempts = _RATE_LIMIT_RETRY_ATTEMPTS if path in _RATE_LIMIT_RETRYABLE_PATHS else 1
        for attempt in range(1, attempts + 1):
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            headers = {"Accept": "application/json"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            req = request.Request(
                url=f"{self._base_url}{path}",
                method=method,
                headers=headers,
                data=data,
            )
            try:
                response = self._request_fn(req, self._cookiejar)
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in {404, 405}:
                    raise DokployBootstrapAuthError(f"endpoint-unavailable:{path}") from exc
                if exc.code == 429 and attempt < attempts:
                    time.sleep(_RATE_LIMIT_RETRY_DELAY_SECONDS)
                    continue
                raise DokployBootstrapAuthError(
                    f"Dokploy auth request to {path} failed with status {exc.code}: "
                    f"{body or exc.reason}."
                ) from exc
            except error.URLError as exc:
                raise DokployBootstrapAuthError(
                    f"Dokploy auth request to {path} failed: {exc.reason}."
                ) from exc
            if isinstance(response, list):
                return response
            if not isinstance(response, dict):
                raise DokployBootstrapAuthError(
                    f"Dokploy auth response from {path} must decode to a JSON object."
                )
            data_payload = response.get("data", response)
            if not isinstance(data_payload, (dict, list)):
                raise DokployBootstrapAuthError(
                    f"Dokploy auth response from {path} must decode to a JSON object."
                )
            return data_payload
        raise DokployBootstrapAuthError(
            f"Dokploy auth request to {path} exhausted rate-limit retries without a response."
        )


def _default_request(req: request.Request, jar: http.cookiejar.CookieJar) -> Any:
    opener = request.build_opener(request.HTTPCookieProcessor(jar))
    with opener.open(req, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _extract_active_organization_id(payload: dict[str, Any]) -> str:
    session = payload.get("session")
    if isinstance(session, dict):
        org_id = session.get("activeOrganizationId")
        if isinstance(org_id, str) and org_id != "":
            return org_id
    raise DokployBootstrapAuthError(
        "Dokploy session response did not expose an active organization id."
    )


def _extract_api_key(payload: dict[str, Any]) -> str:
    direct = payload.get("apiKey")
    if isinstance(direct, str) and direct != "":
        return direct
    if isinstance(direct, dict):
        for key in ("apiKey", "key", "token"):
            value = direct.get(key)
            if isinstance(value, str) and value != "":
                return value
    for key in ("key", "token"):
        value = payload.get(key)
        if isinstance(value, str) and value != "":
            return value
    raise DokployBootstrapAuthError(
        "Dokploy API-key creation response did not include a usable API key."
    )
