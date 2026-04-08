"""Headless local Dokploy auth bootstrap for first-run installs."""

from __future__ import annotations

import http.cookiejar
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import error, request

AUTH_SIGN_IN_PATHS = ("/api/auth/sign-in/email", "/api/auth/sign-in")
AUTH_SIGN_UP_PATHS = ("/api/auth/sign-up/email", "/api/auth/sign-up")
AUTH_SESSION_PATHS = ("/api/user.session", "/api/auth/get-session")
API_KEY_CREATE_PATH = "/api/user.createApiKey"

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

    def _authenticate(self, *, admin_email: str, admin_password: str) -> tuple[str, bool]:
        first_auth_error: DokployBootstrapAuthError | None = None
        for path in AUTH_SIGN_IN_PATHS:
            try:
                self._request_json(
                    "POST",
                    path,
                    {"email": admin_email, "password": admin_password},
                )
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
        for path in AUTH_SESSION_PATHS:
            try:
                payload = self._request_json("GET", path, None)
                return payload, path
            except DokployBootstrapAuthError as error_value:
                if str(error_value).startswith("endpoint-unavailable:"):
                    continue
                raise
        raise DokployBootstrapAuthError(
            "Could not find a working Dokploy session endpoint after authentication."
        )

    def _request_json(self, method: str, path: str, payload: Any | None) -> dict[str, Any]:
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
            raise DokployBootstrapAuthError(
                f"Dokploy auth request to {path} failed with status {exc.code}: "
                f"{body or exc.reason}."
            ) from exc
        except error.URLError as exc:
            raise DokployBootstrapAuthError(
                f"Dokploy auth request to {path} failed: {exc.reason}."
            ) from exc
        if not isinstance(response, dict):
            raise DokployBootstrapAuthError(
                f"Dokploy auth response from {path} must decode to a JSON object."
            )
        data_payload = response.get("data", response)
        if not isinstance(data_payload, dict):
            raise DokployBootstrapAuthError(
                f"Dokploy auth response from {path} must decode to a JSON object."
            )
        return data_payload


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
