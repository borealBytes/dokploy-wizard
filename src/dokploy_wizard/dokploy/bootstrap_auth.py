"""Headless local Dokploy auth bootstrap for first-run installs."""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeGuard
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
        env: str | None = None,
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
        if env is not None:
            self.update_compose(
                admin_email=admin_email,
                admin_password=admin_password,
                compose_id=compose_id,
                env=env,
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
        compose_file: str | None = None,
        env: str | None = None,
    ) -> dict[str, Any]:
        if compose_file is None and env is None:
            raise DokployBootstrapAuthError(
                "Dokploy session compose.update requires compose_file or env."
            )
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        update_payload: dict[str, Any] = {"composeId": compose_id}
        if compose_file is not None:
            update_payload.update(
                {
                    "composeType": "docker-compose",
                    "sourceType": "raw",
                    "composePath": "./docker-compose.yml",
                    "githubId": None,
                    "repository": None,
                    "owner": None,
                    "branch": None,
                    "composeFile": compose_file,
                }
            )
        if env is not None:
            update_payload["env"] = env
        payload = self._request_json(
            "POST",
            "/api/compose.update",
            update_payload,
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session compose.update response must decode to a JSON object."
            )
        return payload

    def list_compose_schedules(
        self,
        *,
        admin_email: str,
        admin_password: str,
        compose_id: str,
    ) -> list[dict[str, Any]]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "GET",
            f"/api/schedule.list?id={compose_id}&scheduleType=compose",
            None,
        )
        if not isinstance(payload, list):
            raise DokployBootstrapAuthError(
                "Dokploy session schedule.list response must decode to a JSON array."
            )
        return payload

    def create_schedule(
        self,
        *,
        admin_email: str,
        admin_password: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/schedule.create",
            {
                "name": name,
                "composeId": compose_id,
                "serviceName": service_name,
                "cronExpression": cron_expression,
                "timezone": timezone,
                "shellType": shell_type,
                "command": command,
                "scheduleType": "compose",
                "enabled": enabled,
            },
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session schedule.create response must decode to a JSON object."
            )
        return payload

    def update_schedule(
        self,
        *,
        admin_email: str,
        admin_password: str,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/schedule.update",
            {
                "scheduleId": schedule_id,
                "name": name,
                "composeId": compose_id,
                "serviceName": service_name,
                "cronExpression": cron_expression,
                "timezone": timezone,
                "shellType": shell_type,
                "command": command,
                "scheduleType": "compose",
                "enabled": enabled,
            },
        )
        if not isinstance(payload, dict):
            raise DokployBootstrapAuthError(
                "Dokploy session schedule.update response must decode to a JSON object."
            )
        return payload

    def delete_schedule(
        self,
        *,
        admin_email: str,
        admin_password: str,
        schedule_id: str,
    ) -> bool:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json("POST", "/api/schedule.delete", {"scheduleId": schedule_id})
        if payload is True:
            return True
        if not isinstance(payload, bool):
            raise DokployBootstrapAuthError(
                "Dokploy session schedule.delete response must decode to a boolean."
            )
        return payload

    def list_ai_providers(
        self,
        *,
        admin_email: str,
        admin_password: str,
    ) -> list[dict[str, Any]]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json("GET", "/api/ai.getAll", None)
        if not isinstance(payload, list):
            raise DokployBootstrapAuthError(
                "Dokploy session ai.getAll response must decode to a JSON array."
            )
        return payload

    def create_ai_provider(
        self,
        *,
        admin_email: str,
        admin_password: str,
        name: str,
        api_url: str,
        api_key: str,
        model: str,
        is_enabled: bool,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/ai.create",
            {
                "name": name,
                "apiUrl": api_url,
                "apiKey": api_key,
                "model": model,
                "isEnabled": is_enabled,
            },
            allow_scalar_data=True,
        )
        if _is_ai_provider_payload(payload):
            return payload
        return self._recover_ai_provider_after_mutation(
            operation="ai.create",
            name=name,
            api_url=api_url,
            api_key=api_key,
            model=model,
            is_enabled=is_enabled,
        )

    def _recover_ai_provider_after_mutation(
        self,
        *,
        operation: str,
        name: str,
        api_url: str,
        api_key: str,
        model: str,
        is_enabled: bool,
        ai_id: str | None = None,
    ) -> dict[str, Any]:
        providers = self._request_json("GET", "/api/ai.getAll", None)
        if not isinstance(providers, list):
            raise DokployBootstrapAuthError(
                f"Dokploy session {operation} recovery list response must decode to a JSON array."
            )
        matches = [
            provider
            for provider in providers
            if _matches_ai_provider(
                provider,
                name=name,
                api_url=api_url,
                api_key=api_key,
                model=model,
                is_enabled=is_enabled,
                ai_id=ai_id,
            )
        ]
        if len(matches) != 1:
            raise DokployBootstrapAuthError(
                f"Dokploy session {operation} succeeded but provider recovery found "
                f"{len(matches)} matching records."
            )
        return matches[0]

    def update_ai_provider(
        self,
        *,
        admin_email: str,
        admin_password: str,
        ai_id: str,
        name: str,
        api_url: str,
        api_key: str,
        model: str,
        is_enabled: bool,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        payload = self._request_json(
            "POST",
            "/api/ai.update",
            {
                "aiId": ai_id,
                "name": name,
                "apiUrl": api_url,
                "apiKey": api_key,
                "model": model,
                "isEnabled": is_enabled,
            },
            allow_scalar_data=True,
        )
        if _is_ai_provider_payload(payload):
            return payload
        return self._recover_ai_provider_after_mutation(
            operation="ai.update",
            ai_id=ai_id,
            name=name,
            api_url=api_url,
            api_key=api_key,
            model=model,
            is_enabled=is_enabled,
        )


    def test_ai_provider_connection(
        self,
        *,
        admin_email: str,
        admin_password: str,
        api_url: str,
        api_key: str,
        model: str,
    ) -> dict[str, Any]:
        self._authenticate(admin_email=admin_email, admin_password=admin_password)
        self._resolve_session()
        try:
            payload = self._request_json(
                "POST",
                "/api/trpc/ai.testConnection?batch=1",
                {
                    "0": {
                        "json": {
                            "apiUrl": api_url,
                            "apiKey": api_key,
                            "model": model,
                        }
                    }
                },
            )
        except DokployBootstrapAuthError as exc:
            raise DokployBootstrapAuthError(
                _redact_known_secrets(str(exc), (api_key,))
            ) from exc
        result = _extract_trpc_json_payload(payload, operation="ai.testConnection")
        if isinstance(result, bool):
            return {"success": result}
        if not isinstance(result, dict):
            raise DokployBootstrapAuthError(
                "Dokploy ai.testConnection JSON payload must decode to a JSON object."
            )
        success = result.get("success")
        if not isinstance(success, bool):
            raise DokployBootstrapAuthError(
                "Dokploy ai.testConnection success must decode to a boolean."
            )
        message = result.get("message")
        if message is not None and not isinstance(message, str):
            raise DokployBootstrapAuthError(
                "Dokploy ai.testConnection message must decode to a string or null."
            )
        return {
            "success": success,
            "message": _redact_known_secrets(message, (api_key,)) if message else None,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Any | None,
        *,
        allow_scalar_data: bool = False,
    ) -> Any:
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
                safe_body = _redact_known_secrets(body or str(exc.reason), ())
                raise DokployBootstrapAuthError(
                    f"Dokploy auth request to {path} failed with status {exc.code}: "
                    f"{safe_body}."
                ) from exc
            except error.URLError as exc:
                raise DokployBootstrapAuthError(
                    f"Dokploy auth request to {path} failed: {exc.reason}."
                ) from exc
            if isinstance(response, list):
                return response
            if allow_scalar_data and response in (True, False, None):
                return response
            if not isinstance(response, dict):
                raise DokployBootstrapAuthError(
                    f"Dokploy auth response from {path} must decode to a JSON object."
                )
            data_payload = response.get("data", response)
            if isinstance(data_payload, (dict, list)):
                return data_payload
            if allow_scalar_data and data_payload in (True, False, None):
                return data_payload
            raise DokployBootstrapAuthError(
                f"Dokploy auth response from {path} must decode to a JSON object."
            )
        raise DokployBootstrapAuthError(
            f"Dokploy auth request to {path} exhausted rate-limit retries without a response."
        )

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

def _extract_trpc_json_payload(payload: Any, *, operation: str) -> Any:
    if not isinstance(payload, list) or not payload:
        raise DokployBootstrapAuthError(
            f"Dokploy {operation} response must decode to a non-empty JSON array."
        )
    first = payload[0]
    if not isinstance(first, dict):
        raise DokployBootstrapAuthError(
            f"Dokploy {operation} batch item must decode to a JSON object."
        )
    error_payload = first.get("error")
    if isinstance(error_payload, dict):
        error_json = error_payload.get("json")
        message = None
        if isinstance(error_json, dict):
            candidate = error_json.get("message")
            if isinstance(candidate, str):
                message = candidate
        if message is None:
            candidate = error_payload.get("message")
            if isinstance(candidate, str):
                message = candidate
        safe_message = _redact_known_secrets(message or str(error_payload), ())
        raise DokployBootstrapAuthError(f"Dokploy {operation} failed: {safe_message}")
    result = first.get("result")
    if not isinstance(result, dict):
        raise DokployBootstrapAuthError(
            f"Dokploy {operation} result must decode to a JSON object."
        )
    data = result.get("data")
    if isinstance(data, dict) and "json" in data:
        return data["json"]
    if data in (True, False):
        return data
    raise DokployBootstrapAuthError(
        f"Dokploy {operation} data must include a JSON payload."
    )


def _redact_known_secrets(text: str | None, secrets: tuple[str, ...]) -> str:
    if not text:
        return ""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<REDACTED>")
    redacted = re.sub(r"sk-[A-Za-z0-9._\-]+", "sk-<REDACTED>", redacted)
    redacted = re.sub(r"ghp_[A-Za-z0-9_]+", "ghp_<REDACTED>", redacted)
    return redacted


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


def _is_ai_provider_payload(payload: Any) -> TypeGuard[dict[str, Any]]:
    if not isinstance(payload, dict):
        return False
    return (
        _is_non_empty_string(payload.get("aiId"))
        and _is_non_empty_string(payload.get("name"))
        and _is_non_empty_string(payload.get("apiUrl"))
        and _is_non_empty_string(payload.get("apiKey"))
        and _is_non_empty_string(payload.get("model"))
        and isinstance(payload.get("isEnabled"), bool)
    )


def _matches_ai_provider(
    payload: Any,
    *,
    name: str,
    api_url: str,
    api_key: str,
    model: str,
    is_enabled: bool,
    ai_id: str | None,
) -> TypeGuard[dict[str, Any]]:
    if not _is_ai_provider_payload(payload):
        return False
    if ai_id is not None and payload["aiId"] != ai_id:
        return False
    return (
        payload["name"] == name
        and payload["apiUrl"] == api_url
        and payload["apiKey"] == api_key
        and payload["model"] == model
        and payload["isEnabled"] is is_enabled
    )


def _is_non_empty_string(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and value != ""
