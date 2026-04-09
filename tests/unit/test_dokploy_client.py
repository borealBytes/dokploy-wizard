# pyright: reportMissingImports=false

from __future__ import annotations

import json
from typing import cast
from urllib import request

import pytest

from dokploy_wizard.dokploy import DokployApiClient, DokployApiError


def test_dokploy_client_uses_x_api_key_and_api_paths() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {"data": [{"projectId": "proj-1", "name": "wizard", "environments": []}]}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    projects = client.list_projects()

    assert captured["url"] == "https://dokploy.example.com/api/project.all"
    assert captured["method"] == "GET"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-api-key"] == "dokp-key-123"
    assert projects[0].project_id == "proj-1"


def test_dokploy_client_creates_compose_with_json_payload() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        captured["url"] = req.full_url
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {"data": {"composeId": "cmp-1", "name": "wizard-shared"}}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    record = client.create_compose(
        name="wizard-shared",
        environment_id="env-1",
        compose_file="services:\n  db:\n    image: postgres:16-alpine\n",
        app_name="wizard-shared",
    )

    assert captured["url"] == "https://dokploy.example.com/api/compose.create"
    body = json.loads(str(captured["body"]))
    assert body["environmentId"] == "env-1"
    assert body["composeType"] == "docker-compose"
    assert record.compose_id == "cmp-1"


def test_dokploy_client_rejects_invalid_response_shapes() -> None:
    client = DokployApiClient(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        request_fn=lambda req: {"data": {"unexpected": True}},
    )

    with pytest.raises(DokployApiError, match="project.all response must be a list"):
        client.list_projects()


def test_dokploy_client_accepts_root_json_array_responses() -> None:
    client = DokployApiClient(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        request_fn=lambda req: [{"projectId": "proj-1", "name": "wizard", "environments": []}],
    )

    projects = client.list_projects()

    assert projects[0].project_id == "proj-1"
