from __future__ import annotations

import io
import ssl
from urllib import request as urlrequest

import pytest

import dokploy_wizard.dokploy.coder_template_migration_runtime as runtime
from dokploy_wizard.dokploy.coder_migration_api import (
    CoderHttpRequest,
    CoderHttpResponse,
    UrllibCoderTransport,
)


class _Transport:
    def send(self, _request: CoderHttpRequest) -> CoderHttpResponse:
        return CoderHttpResponse(status=200, body=b"{}")


class _MigrationApi:
    def default_organization_id(self) -> str:
        return "organization-id"


class _Response(io.BytesIO):
    status = 200


class _Opener:
    def __init__(self, requests: list[urlrequest.Request]) -> None:
        self._requests = requests

    def open(self, request: urlrequest.Request, *, timeout: int) -> _Response:
        assert timeout == 30
        self._requests.append(request)
        return _Response(b"{}")


def test_preflight_routes_coder_api_through_local_traefik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    endpoint_configs: list[tuple[str, str | None, bool]] = []

    def capture_transport(
        base_url: str,
        *,
        host_header: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> _Transport:
        endpoint_configs.append((base_url, host_header, ssl_context is not None))
        return _Transport()

    def migration_api(*, transport: _Transport, session_token: str) -> _MigrationApi:
        assert transport is not None
        assert session_token == "session-token"
        return _MigrationApi()

    def read_inventory(_api: _MigrationApi, organization_id: str) -> None:
        assert organization_id == "organization-id"

    monkeypatch.setattr(runtime, "UrllibCoderTransport", capture_transport)
    monkeypatch.setattr(runtime, "CoderMigrationApi", migration_api)
    monkeypatch.setattr(runtime, "read_coder_migration_inventory", read_inventory)

    # When
    runtime.execute_template_migration_preflight("coder.example.test", "session-token")

    # Then
    assert endpoint_configs == [("https://127.0.0.1", "coder.example.test", True)]


def test_transport_applies_host_header_and_tls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    requests: list[urlrequest.Request] = []
    contexts: list[ssl.SSLContext | None] = []
    context = ssl.create_default_context()

    def build_opener(*requested_handlers: urlrequest.BaseHandler) -> _Opener:
        assert len(requested_handlers) == 2
        return _Opener(requests)

    def https_handler(*, context: ssl.SSLContext | None) -> urlrequest.BaseHandler:
        contexts.append(context)
        return urlrequest.BaseHandler()

    monkeypatch.setattr(urlrequest, "build_opener", build_opener)
    monkeypatch.setattr(urlrequest, "HTTPSHandler", https_handler)
    transport = UrllibCoderTransport(
        "https://127.0.0.1",
        host_header="coder.example.test",
        ssl_context=context,
    )

    # When
    response = transport.send(
        CoderHttpRequest(method="GET", path="/api/v2/templates", body=None, headers=())
    )

    # Then
    assert response == CoderHttpResponse(status=200, body=b"{}")
    assert len(requests) == 1
    assert requests[0].full_url == "https://127.0.0.1/api/v2/templates"
    assert requests[0].get_header("Host") == "coder.example.test"
    assert contexts == [context]
