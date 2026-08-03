from __future__ import annotations

import json
from typing import TypeAlias

import pytest

from dokploy_wizard.dokploy.coder_migration_api import (
    CoderHttpRequest,
    CoderHttpResponse,
    CoderMigrationApi,
)
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderApiError,
    CoderProtocolError,
    parse_build,
    parse_coder_error,
)

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class FakeTransport:
    def __init__(self, responses: tuple[CoderHttpResponse, ...]) -> None:
        self._responses = list(responses)
        self.requests: list[CoderHttpRequest] = []

    def send(self, request: CoderHttpRequest) -> CoderHttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _json_response(status: int, value: JsonValue) -> CoderHttpResponse:
    return CoderHttpResponse(status=status, body=json.dumps(value, separators=(",", ":")).encode())


def test_lists_templates_with_authenticated_bare_array_contract() -> None:
    # Given
    transport = FakeTransport(
        (
            _json_response(
                200,
                [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "organization_id": "22222222-2222-2222-2222-222222222222",
                        "name": "ubuntu-vscode",
                    }
                ],
            ),
        )
    )
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When
    templates = api.list_templates("22222222-2222-2222-2222-222222222222")

    # Then
    assert templates[0].name == "ubuntu-vscode"
    assert transport.requests == [
        CoderHttpRequest(
            method="GET",
            path="/api/v2/templates?q=organization%3A%2222222222-2222-2222-2222-222222222222%22",
            body=None,
            headers=(
                ("Accept", "application/json"),
                ("Content-Type", "application/json"),
                ("Coder-Session-Token", "session-token"),
            ),
        )
    ]


def test_paginates_workspace_envelope_to_exact_count() -> None:
    # Given
    workspace: dict[str, JsonValue] = {
        "id": "33333333-3333-3333-3333-333333333333",
        "template_id": "11111111-1111-1111-1111-111111111111",
        "name": "stopped-workspace",
        "latest_build": {
            "id": "44444444-4444-4444-4444-444444444444",
            "build_number": 1,
            "transition": "stop",
            "status": "stopped",
            "created_at": "2026-07-27T00:00:00Z",
        },
    }
    first_page: list[JsonValue] = [
        {**workspace, "id": f"33333333-3333-3333-3333-{index:012d}"}
        for index in range(100)
    ]
    final_page: list[JsonValue] = [
        {**workspace, "id": "33333333-3333-3333-3333-000000000100"}
    ]
    transport = FakeTransport(
        (
            _json_response(200, {"workspaces": first_page, "count": 101}),
            _json_response(200, {"workspaces": final_page, "count": 101}),
        )
    )
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When
    workspaces = api.list_workspaces()

    # Then
    assert len(workspaces) == 101
    assert [request.path for request in transport.requests] == [
        "/api/v2/workspaces?q=&limit=100&offset=0",
        "/api/v2/workspaces?q=&limit=100&offset=100",
    ]


def test_rejects_structured_coder_error_without_losing_validation_details() -> None:
    # Given
    transport = FakeTransport(
        (
            _json_response(
                409,
                {
                    "message": "conflict",
                    "detail": "build changed",
                    "validations": [{"field": "transition", "detail": "invalid"}],
                },
            ),
        )
    )
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When / Then
    with pytest.raises(CoderApiError) as raised:
        api.rename_template(
            "11111111-1111-1111-1111-111111111111", "renamed-template"
        )
    assert raised.value.status == 409
    assert raised.value.validations[0].field == "transition"


def test_rejects_partial_workspace_pagination_envelope() -> None:
    # Given
    transport = FakeTransport((_json_response(200, {"workspaces": []}),))
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When / Then
    with pytest.raises(CoderProtocolError):
        api.list_workspaces()


def test_rename_template_happy_uses_exact_patch_path_and_body() -> None:
    # Given
    transport = FakeTransport(
        (
            _json_response(
                200,
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "organization_id": "22222222-2222-2222-2222-222222222222",
                    "name": "renamed-template",
                },
            ),
        )
    )
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When
    template = api.rename_template(
        "11111111-1111-1111-1111-111111111111", "renamed-template"
    )

    # Then
    assert template.name == "renamed-template"
    assert transport.requests[0].method == "PATCH"
    assert transport.requests[0].path == "/api/v2/templates/11111111-1111-1111-1111-111111111111"
    assert transport.requests[0].body == b'{"name":"renamed-template"}'


def test_workspace_proof_mutations_use_exact_coder_api_contracts() -> None:
    # Given
    workspace: JsonValue = {
        "id": "33333333-3333-3333-3333-333333333333",
        "template_id": "11111111-1111-1111-1111-111111111111",
        "name": "proof-workspace",
        "latest_build": {
            "id": "44444444-4444-4444-4444-444444444444",
            "build_number": 1,
            "transition": "start",
            "status": "pending",
            "created_at": "2026-07-28T12:34:56Z",
        },
    }
    stopped: JsonValue = {
        "id": "55555555-5555-5555-5555-555555555555",
        "build_number": 2,
        "transition": "stop",
        "status": "pending",
        "created_at": "2026-07-28T12:35:56Z",
    }
    transport = FakeTransport((_json_response(201, workspace), _json_response(201, stopped)))
    api = CoderMigrationApi(transport=transport, session_token="session-token")

    # When
    created = api.create_workspace(
        template_id="11111111-1111-1111-1111-111111111111",
        workspace_name="proof-workspace",
    )
    build = api.submit_workspace_transition(
        "33333333-3333-3333-3333-333333333333",
        "stop",
    )

    # Then
    assert created.name == "proof-workspace"
    assert build.transition == "stop"
    assert [(request.method, request.path, request.body) for request in transport.requests] == [
        (
            "POST",
            "/api/v2/users/me/workspaces",
            b'{"name":"proof-workspace","template_id":"11111111-1111-1111-1111-111111111111"}',
        ),
        (
            "POST",
            "/api/v2/workspaces/33333333-3333-3333-3333-333333333333/builds",
            b'{"transition":"stop"}',
        ),
    ]


def test_normalizes_fractional_coder_build_timestamp_for_receipt_canonicality() -> None:
    # Given
    value: JsonValue = {
        "id": "11111111-1111-1111-1111-111111111111",
        "build_number": 1,
        "transition": "stop",
        "status": "stopped",
        "created_at": "2026-07-28T12:34:56.123456Z",
    }

    # When
    build = parse_build(value)

    # Then
    assert build.created_at == "2026-07-28T12:34:56Z"


def test_accepts_coder_error_without_optional_validation_array() -> None:
    # Given
    value: JsonValue = {"message": "conflict", "detail": None}

    # When
    error = parse_coder_error(409, value)

    # Then
    assert error.validations == ()


def test_rejects_coder_error_with_null_validation_array() -> None:
    # Given
    value: JsonValue = {"message": "conflict", "detail": None, "validations": None}

    # When / Then
    with pytest.raises(CoderProtocolError, match="validations is null"):
        parse_coder_error(409, value)
