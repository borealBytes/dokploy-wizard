from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib import request

import pytest

from dokploy_wizard.proof import model_sync_coder_api, model_sync_host_b
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1


class _Response:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps({"session_token": "session"}).encode()


class _Opener:
    def __init__(self, requests: list[request.Request]) -> None:
        self._requests = requests

    def open(self, value: request.Request, *, timeout: int) -> _Response:
        assert timeout == 30
        self._requests.append(value)
        return _Response()


def _context() -> Task1ProofContextV1:
    digest = "a" * 64
    return Task1ProofContextV1(
        context_id="b" * 32,
        source_env_sha256=digest,
        normalized_env_sha256=digest,
        overlay_env_sha256=digest,
        uploaded_env_sha256=digest,
        namespace_sha256=digest,
        expected_restored_source_sha256=digest,
        source_env_mode=0o600,
        root_domain="example.test",
        stack_name="proof-stack",
        tunnel_name="proof-tunnel",
        dokploy_subdomain="dokploy-proof",
        coder_subdomain="coder-proof",
        seaweedfs_subdomain="seaweedfs-proof",
        litellm_admin_subdomain="litellm-proof",
    )


def _install_api_fakes(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[request.Request],
    *,
    networks: dict[str, JsonValue],
) -> None:
    monkeypatch.setattr(
        model_sync_coder_api,
        "active_task1_proof_context",
        _context,
    )
    monkeypatch.setattr(
        model_sync_coder_api,
        "_coder_container_name",
        lambda _service: "coder-container",
    )
    monkeypatch.setattr(
        model_sync_coder_api,
        "run_bounded_process",
        lambda *_args, **_kwargs: json.dumps(
            [{"NetworkSettings": {"Networks": networks}}]
        ).encode(),
    )
    monkeypatch.setattr(request, "build_opener", lambda *_handlers: _Opener(captured))


def test_api_uses_shared_internal_coder_route_when_task1_context_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[request.Request] = []
    _install_api_fakes(
        monkeypatch,
        captured,
        networks={"proof-stack-shared": {"IPAddress": "172.20.0.7"}},
    )

    token = model_sync_coder_api.coder_login("coder-proof.example.test", "admin", "secret")

    assert token == "session"
    assert captured[0].full_url == "http://172.20.0.7:3000/api/v2/users/login"
    assert captured[0].get_header("Host") == "coder-proof.example.test"


def test_api_preserves_public_route_without_task1_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[request.Request] = []
    monkeypatch.setattr(
        model_sync_coder_api,
        "active_task1_proof_context",
        lambda: None,
    )
    monkeypatch.setattr(request, "build_opener", lambda *_handlers: _Opener(captured))

    model_sync_coder_api.api("coder.example.test", None, "/api/v2/users/me")

    assert captured[0].full_url == "https://coder.example.test/api/v2/users/me"


def test_api_rejects_missing_task1_shared_network_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[request.Request] = []
    _install_api_fakes(
        monkeypatch,
        captured,
        networks={"proof-stack-default": {"IPAddress": "172.19.0.3"}},
    )

    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api.api("coder-proof.example.test", None, "/api/v2/users/me")

    assert error.value.stage == "route"
    assert captured == []


def test_nullable_api_accepts_null_while_strict_api_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_sync_coder_api, "_api_value", lambda *_args: None)

    assert model_sync_coder_api.nullable_api("coder.example.test", None, "/presets") is None
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api.api("coder.example.test", None, "/presets")

    assert error.value.stage == "payload"


def test_templates_resolve_active_version_name_from_version_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def fetch(
        _hostname: str,
        _token: str | None,
        path: str,
        _body: dict[str, str] | None = None,
    ) -> JsonValue:
        paths.append(path)
        if path == "/api/v2/templates":
            return [
                {
                    "id": "template-id",
                    "name": "template-name",
                    "active_version_id": "version-id",
                }
            ]
        assert path == "/api/v2/templateversions/version-id"
        return {"name": "version-name"}

    monkeypatch.setattr(model_sync_host_b, "_api", fetch)

    templates = model_sync_host_b._templates("coder.example.test", "session")

    assert templates[0]["active_version_name"] == "version-name"
    assert paths == ["/api/v2/templates", "/api/v2/templateversions/version-id"]


def test_workspaces_resolve_template_version_from_latest_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fetch(
        _hostname: str,
        _token: str | None,
        path: str,
        _body: dict[str, str] | None = None,
    ) -> JsonValue:
        assert path == "/api/v2/workspaces?q=&limit=100&offset=0"
        return {
            "count": 1,
            "workspaces": [
                {
                    "id": "workspace-id",
                    "name": "workspace-name",
                    "template_id": "template-id",
                    "latest_build": {"template_version_id": "version-id"},
                }
            ],
        }

    monkeypatch.setattr(model_sync_host_b, "_api", fetch)
    monkeypatch.setattr(
        model_sync_host_b,
        "_legacy_renderer",
        lambda *_args: model_sync_host_b.LegacyRenderer("base", "key", "alias", (), ("model",)),
    )
    monkeypatch.setattr(model_sync_host_b, "_primary_pointer", lambda *_args: {})

    workspaces = model_sync_host_b._workspaces(
        "coder.example.test",
        "session",
        "coder-container",
        [
            {
                "id": "template-id",
                "name": "ubuntu-vscode",
                "active_version_id": "version-id",
                "active_version_name": "version-name",
                "rendered_source_sha256": "a" * 64,
            }
        ],
        {},
        Path("."),
        "proof-stack",
    )

    assert workspaces[0]["template_version_id"] == "version-id"
