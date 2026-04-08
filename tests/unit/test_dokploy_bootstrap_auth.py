# pyright: reportMissingImports=false

from __future__ import annotations

import http.cookiejar
from email.message import Message
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


def test_bootstrap_auth_fails_clearly_when_no_auth_routes_work() -> None:
    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        del jar
        raise error.HTTPError(req.full_url, 404, "Not found", hdrs=Message(), fp=None)

    with pytest.raises(DokployBootstrapAuthError, match="working Dokploy auth endpoint"):
        DokployBootstrapAuthClient(
            base_url="http://127.0.0.1:3000",
            request_fn=fake_request,
        ).ensure_api_key(admin_email="admin@example.com", admin_password="secret-123")
