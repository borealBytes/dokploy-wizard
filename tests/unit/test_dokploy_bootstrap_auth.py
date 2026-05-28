# pyright: reportMissingImports=false

from __future__ import annotations

import http.cookiejar
import json
from email.message import Message
from io import BytesIO
from urllib import error, request

import pytest

from dokploy_wizard.dokploy import DokployBootstrapAuthClient, DokployBootstrapAuthError


def test_bootstrap_auth_signs_in_and_creates_api_key() -> None:
    seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/user.createApiKey"):
            return {"apiKey": "dokp-key-123"}
        raise AssertionError(req.full_url)

    result = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")

    assert result.api_key == "dokp-key-123"
    assert result.organization_id == "org-1"
    assert result.used_sign_up is False
    assert seen[0].endswith("/api/auth/sign-in/email")


def test_bootstrap_auth_falls_back_to_signup_when_signin_fails() -> None:
    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            raise error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=Message(), fp=None)
        if req.full_url.endswith("/api/auth/sign-up/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/user.createApiKey"):
            return {"apiKey": {"key": "dokp-key-123"}}
        raise AssertionError(req.full_url)

    result = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")

    assert result.used_sign_up is True
    assert result.api_key == "dokp-key-123"


def test_bootstrap_auth_tries_fallback_auth_routes_on_404() -> None:
    seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            raise error.HTTPError(req.full_url, 404, "Not found", hdrs=Message(), fp=None)
        if req.full_url.endswith("/api/auth/sign-in"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/user.createApiKey"):
            return {"key": "dokp-key-123"}
        raise AssertionError(req.full_url)

    result = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")

    assert result.auth_path == "/api/auth/sign-in"
    assert seen[0].endswith("/api/auth/sign-in/email")
    assert seen[1].endswith("/api/auth/sign-in")


def test_bootstrap_auth_retries_rate_limited_signin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    sleep_calls: list[float] = []
    attempts = {"count": 0}

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise error.HTTPError(
                    req.full_url,
                    429,
                    "Too Many Requests",
                    hdrs=Message(),
                    fp=None,
                )
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/user.createApiKey"):
            return {"apiKey": "dokp-key-123"}
        raise AssertionError(req.full_url)

    monkeypatch.setattr("dokploy_wizard.dokploy.bootstrap_auth.time.sleep", sleep_calls.append)

    result = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")

    assert result.api_key == "dokp-key-123"
    assert attempts["count"] == 3
    assert sleep_calls == [5.0, 5.0]


def test_bootstrap_auth_fails_clearly_when_no_auth_routes_work() -> None:
    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        raise error.HTTPError(req.full_url, 404, "Not found", hdrs=Message(), fp=None)

    with pytest.raises(DokployBootstrapAuthError, match="working Dokploy auth endpoint"):
        DokployBootstrapAuthClient(
            base_url="http://127.0.0.1:3000",
            request_fn=fake_request,
        ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")


def test_bootstrap_auth_list_projects_accepts_array_response() -> None:
    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/project.all"):
            return [{"projectId": "proj-1", "name": "wizard", "environments": []}]
        raise AssertionError(req.full_url)

    projects = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).list_projects(admin_email="admin@example.com", admin_password="secret-123")

    assert projects == [{"projectId": "proj-1", "name": "wizard", "environments": []}]


def test_bootstrap_auth_reuses_authenticated_session_across_fallback_calls() -> None:
    seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/project.all"):
            return [{"projectId": "proj-1", "name": "wizard", "environments": []}]
        if req.full_url.endswith("/api/compose.deploy"):
            return {"success": True, "composeId": "cmp-1", "message": None}
        raise AssertionError(req.full_url)

    client = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    )

    client.list_projects(admin_email="admin@example.com", admin_password="secret-123")
    client.deploy_compose(
        admin_email="admin@example.com",
        admin_password="secret-123",
        compose_id="cmp-1",
        title="probe",
        description="reuse-session",
    )

    assert seen.count("http://127.0.0.1:3000/api/auth/sign-in/email") == 1
    assert seen.count("http://127.0.0.1:3000/api/user.session") == 1


def test_bootstrap_auth_create_compose_posts_raw_compose_payload() -> None:
    requests_seen: list[tuple[str, dict[str, object]]] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/compose.update"):
            body = req.data
            assert isinstance(body, bytes)
            requests_seen.append((req.full_url, json.loads(body.decode("utf-8"))))
            return {"composeId": "cmp-1", "name": "wizard-headscale"}
        if req.full_url.endswith("/api/compose.create"):
            body = req.data
            assert isinstance(body, bytes)
            requests_seen.append((req.full_url, json.loads(body.decode("utf-8"))))
            return {"composeId": "cmp-1", "name": "wizard-headscale"}
        raise AssertionError(req.full_url)

    payload = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).create_compose(
        admin_email="admin@example.com",
        admin_password="secret-123",
        name="wizard-headscale",
        environment_id="env-1",
        compose_file="services:\n  app:\n    image: example\n",
        app_name="wizard-headscale",
    )

    assert payload == {"composeId": "cmp-1", "name": "wizard-headscale"}
    assert requests_seen[0] == (
        "http://127.0.0.1:3000/api/compose.create",
        {
            "name": "wizard-headscale",
            "environmentId": "env-1",
            "composeType": "docker-compose",
            "appName": "wizard-headscale",
        },
    )
    assert requests_seen[1][0] == "http://127.0.0.1:3000/api/compose.update"
    update_body = requests_seen[1][1]
    assert update_body["composeId"] == "cmp-1"
    assert update_body["sourceType"] == "raw"
    assert update_body["composePath"] == "./docker-compose.yml"
    assert update_body["githubId"] is None
    assert update_body["repository"] is None
    assert update_body["owner"] is None
    assert update_body["branch"] is None


def test_bootstrap_auth_ai_provider_methods_use_session_endpoints() -> None:
    requests_seen: list[tuple[str, str, object | None]] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        body = json.loads(req.data.decode("utf-8")) if isinstance(req.data, bytes) else None
        requests_seen.append((req.get_method(), req.full_url, body))
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/ai.getAll"):
            return [
                {
                    "aiId": "ai-1",
                    "name": "Dokploy Wizard LiteLLM",
                    "apiUrl": "http://litellm:4000/v1",
                    "apiKey": "sk-dokploy-ai",
                    "model": "local/model",
                    "isEnabled": True,
                }
            ]
        if req.full_url.endswith("/api/ai.create"):
            assert isinstance(body, dict)
            return {"aiId": "ai-2", **body}
        if req.full_url.endswith("/api/ai.update"):
            assert isinstance(body, dict)
            return body
        raise AssertionError(req.full_url)

    client = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    )

    providers = client.list_ai_providers(
        admin_email="admin@example.com",
        admin_password="secret-123",
    )
    created = client.create_ai_provider(
        admin_email="admin@example.com",
        admin_password="secret-123",
        name="Dokploy Wizard LiteLLM",
        api_url="http://litellm:4000/v1",
        api_key="sk-dokploy-ai",
        model="local/model",
        is_enabled=True,
    )
    updated = client.update_ai_provider(
        admin_email="admin@example.com",
        admin_password="secret-123",
        ai_id="ai-2",
        name="Dokploy Wizard LiteLLM",
        api_url="http://litellm:4000/v1",
        api_key="sk-dokploy-ai",
        model="local/model",
        is_enabled=True,
    )

    assert providers[0]["aiId"] == "ai-1"
    assert created["aiId"] == "ai-2"
    assert updated["aiId"] == "ai-2"
    assert requests_seen[2] == ("GET", "http://127.0.0.1:3000/api/ai.getAll", None)
    assert requests_seen[3] == (
        "POST",
        "http://127.0.0.1:3000/api/ai.create",
        {
            "name": "Dokploy Wizard LiteLLM",
            "apiUrl": "http://litellm:4000/v1",
            "apiKey": "sk-dokploy-ai",
            "model": "local/model",
            "isEnabled": True,
        },
    )
    assert requests_seen[4] == (
        "POST",
        "http://127.0.0.1:3000/api/ai.update",
        {
            "aiId": "ai-2",
            "name": "Dokploy Wizard LiteLLM",
            "apiUrl": "http://litellm:4000/v1",
            "apiKey": "sk-dokploy-ai",
            "model": "local/model",
            "isEnabled": True,
        },
    )


def test_bootstrap_auth_ai_create_recovers_provider_after_list_success_response() -> None:
    requests_seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        requests_seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/ai.create"):
            return []
        if req.full_url.endswith("/api/ai.getAll"):
            return [
                {
                    "aiId": "ai-1",
                    "name": "Other Provider",
                    "apiUrl": "http://other:4000/v1",
                    "apiKey": "sk-other",
                    "model": "other/model",
                    "isEnabled": True,
                },
                {
                    "aiId": "ai-2",
                    "name": "Dokploy Wizard LiteLLM",
                    "apiUrl": "http://litellm:4000/v1",
                    "apiKey": "sk-dokploy-ai",
                    "model": "local/model",
                    "isEnabled": True,
                },
            ]
        raise AssertionError(req.full_url)

    created = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).create_ai_provider(
        admin_email="admin@example.com",
        admin_password="secret-123",
        name="Dokploy Wizard LiteLLM",
        api_url="http://litellm:4000/v1",
        api_key="sk-dokploy-ai",
        model="local/model",
        is_enabled=True,
    )

    assert created["aiId"] == "ai-2"
    assert requests_seen == [
        "http://127.0.0.1:3000/api/auth/sign-in/email",
        "http://127.0.0.1:3000/api/user.session",
        "http://127.0.0.1:3000/api/ai.create",
        "http://127.0.0.1:3000/api/ai.getAll",
    ]


def test_bootstrap_auth_ai_update_recovers_provider_after_scalar_success_envelope() -> None:
    requests_seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        requests_seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/ai.update"):
            return {"data": True}
        if req.full_url.endswith("/api/ai.getAll"):
            return [
                {
                    "aiId": "ai-2",
                    "name": "Dokploy Wizard LiteLLM",
                    "apiUrl": "http://litellm:4000/v1",
                    "apiKey": "sk-dokploy-ai",
                    "model": "local/model",
                    "isEnabled": True,
                }
            ]
        raise AssertionError(req.full_url)

    updated = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).update_ai_provider(
        admin_email="admin@example.com",
        admin_password="secret-123",
        ai_id="ai-2",
        name="Dokploy Wizard LiteLLM",
        api_url="http://litellm:4000/v1",
        api_key="sk-dokploy-ai",
        model="local/model",
        is_enabled=True,
    )

    assert updated["aiId"] == "ai-2"
    assert requests_seen == [
        "http://127.0.0.1:3000/api/auth/sign-in/email",
        "http://127.0.0.1:3000/api/user.session",
        "http://127.0.0.1:3000/api/ai.update",
        "http://127.0.0.1:3000/api/ai.getAll",
    ]


def test_bootstrap_auth_assign_domain_server_uses_trpc_batch_shape() -> None:
    requests_seen: list[tuple[str, object]] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/trpc/settings.assignDomainServer?batch=1"):
            body = req.data
            assert isinstance(body, bytes)
            requests_seen.append((req.full_url, json.loads(body.decode("utf-8"))))
            return [{"result": {"data": {"json": {"host": "dokploy.example.com", "https": True}}}}]
        raise AssertionError(req.full_url)

    payload = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).assign_domain_server(
        admin_email="admin@example.com",
        admin_password="secret-123",
        host="dokploy.example.com",
        certificate_type="none",
        lets_encrypt_email="",
        https=True,
    )

    assert payload == {"host": "dokploy.example.com", "https": True}
    assert requests_seen == [
        (
            "http://127.0.0.1:3000/api/trpc/settings.assignDomainServer?batch=1",
            {
                "0": {
                    "json": {
                        "host": "dokploy.example.com",
                        "certificateType": "none",
                        "letsEncryptEmail": "",
                        "https": True,
                    }
                }
            },
        )
    ]


def test_bootstrap_auth_delete_project_posts_expected_payload() -> None:
    requests_seen: list[tuple[str, object]] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/project.remove"):
            body = req.data
            assert isinstance(body, bytes)
            requests_seen.append((req.full_url, json.loads(body.decode("utf-8"))))
            return {"projectId": "proj-1", "name": "wizard-probe"}
        raise AssertionError(req.full_url)

    payload = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).delete_project(
        admin_email="admin@example.com",
        admin_password="secret-123",
        project_id="proj-1",
    )

    assert payload == {"projectId": "proj-1", "name": "wizard-probe"}
    assert requests_seen == [("http://127.0.0.1:3000/api/project.remove", {"projectId": "proj-1"})]


def test_bootstrap_auth_ai_provider_test_connection_uses_trpc_batch_shape() -> None:
    requests_seen: list[tuple[str, object]] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/trpc/ai.testConnection?batch=1"):
            body = req.data
            assert isinstance(body, bytes)
            decoded = json.loads(body.decode("utf-8"))
            requests_seen.append((req.full_url, decoded))
            return [{"result": {"data": {"json": {"success": True, "message": "ok"}}}}]
        raise AssertionError(req.full_url)

    result = DokployBootstrapAuthClient(
        base_url="http://127.0.0.1:3000",
        request_fn=fake_request,
    ).test_ai_provider_connection(
        admin_email="admin@example.com",
        admin_password="secret-123",
        api_url="http://litellm:4000/v1",
        api_key="sk-dokploy-ai",
        model="local/model",
    )

    assert result == {"success": True, "message": "ok"}
    assert requests_seen == [
        (
            "http://127.0.0.1:3000/api/trpc/ai.testConnection?batch=1",
            {
                "0": {
                    "json": {
                        "apiUrl": "http://litellm:4000/v1",
                        "apiKey": "sk-dokploy-ai",
                        "model": "local/model",
                    }
                }
            },
        )
    ]


def test_bootstrap_auth_ai_provider_test_connection_redacts_failed_secret() -> None:
    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        if req.full_url.endswith("/api/auth/sign-in/email"):
            return {"ok": True}
        if req.full_url.endswith("/api/user.session"):
            return {"session": {"activeOrganizationId": "org-1"}}
        if req.full_url.endswith("/api/trpc/ai.testConnection?batch=1"):
            raise error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=Message(),
                fp=BytesIO(b'{"message":"fetch failed for sk-dokploy-ai"}'),
            )
        raise AssertionError(req.full_url)

    with pytest.raises(DokployBootstrapAuthError) as exc:
        DokployBootstrapAuthClient(
            base_url="http://127.0.0.1:3000",
            request_fn=fake_request,
        ).test_ai_provider_connection(
            admin_email="admin@example.com",
            admin_password="secret-123",
            api_url="http://litellm:4000/v1",
            api_key="sk-dokploy-ai",
            model="local/model",
        )

    rendered = str(exc.value)
    assert "sk-dokploy-ai" not in rendered
    assert "sk-<REDACTED>" in rendered
