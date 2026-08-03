from __future__ import annotations

import json
from typing import TypeAlias

from dokploy_wizard.dokploy.coder_migration_api import (
    CoderHttpRequest,
    CoderHttpResponse,
    CoderMigrationApi,
)

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class _Transport:
    def __init__(self, response: CoderHttpResponse) -> None:
        self._response = response

    def send(self, _request: CoderHttpRequest) -> CoderHttpResponse:
        return self._response


def test_workspace_builds_are_canonicalized_oldest_first() -> None:
    # Given
    builds: JsonValue = [
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "build_number": 2,
            "transition": "stop",
            "status": "stopped",
            "created_at": "2026-08-03T00:01:00Z",
        },
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "build_number": 1,
            "transition": "start",
            "status": "running",
            "created_at": "2026-08-03T00:00:00Z",
        },
    ]
    response = CoderHttpResponse(
        status=200,
        body=json.dumps(builds, separators=(",", ":")).encode(),
    )
    api = CoderMigrationApi(transport=_Transport(response), session_token="session-token")

    # When
    observed = api.list_workspace_builds("33333333-3333-3333-3333-333333333333")

    # Then
    assert tuple(build.build_number for build in observed) == (1, 2)
