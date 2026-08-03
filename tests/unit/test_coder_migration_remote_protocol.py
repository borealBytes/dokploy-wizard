from __future__ import annotations

import pytest

from tests.integration.coder_migration_remote_build_detail import parse_workspace_build_detail
from tests.integration.coder_migration_remote_protocol import (
    RemoteCoderProtocolError,
    RemoteCoderResponse,
    parse_created_user,
    parse_first_user_probe,
    workspace_build_failure_category,
)


def test_first_user_probe_rejects_404_without_coder_build_version_header() -> None:
    # Given
    response = RemoteCoderResponse(status=404, body=b'{"message":"not found"}', headers=())

    # When / Then
    with pytest.raises(RemoteCoderProtocolError, match="build-version"):
        parse_first_user_probe(response)


def test_first_user_creation_parses_pinned_coder_user_and_organization_ids() -> None:
    # Given
    response = RemoteCoderResponse(
        status=201,
        body=(
            b'{"user_id":"11111111-1111-1111-1111-111111111111",'
            b'"organization_id":"22222222-2222-2222-2222-222222222222"}'
        ),
        headers=(),
    )

    # When
    created = parse_created_user(response)

    # Then
    assert str(created.user_id) == "11111111-1111-1111-1111-111111111111"
    assert str(created.organization_id) == "22222222-2222-2222-2222-222222222222"


def test_workspace_build_failure_category_discards_unknown_error_code() -> None:
    # Given
    response = RemoteCoderResponse(
        status=200,
        body=b'{"job":{"error_code":"unrecognized"}}',
        headers=(),
    )

    # When
    category = workspace_build_failure_category(response)

    # Then
    assert category == "workspace-build-unknown"


def test_workspace_build_detail_classifies_agent_connectivity_without_retaining_error_text(
) -> None:
    # Given
    response = RemoteCoderResponse(
        status=200,
        body=(
            b'{"job":{"status":"failed","error":"secret-bearing detail",'
            b'"error_code":"agent_connection_failed","worker_id":"worker"},'
            b'"resources":[{"agents":[{"status":"connecting"}]}]}'
        ),
        headers=(),
    )

    # When
    detail = parse_workspace_build_detail(response)

    # Then
    assert detail.http_category == "ok"
    assert detail.job_status_category == "failed"
    assert detail.error_code_category == "agent"
    assert detail.error_code_present is True
    assert detail.job_error_present is True
    assert detail.worker_assigned is True
    assert detail.resource_category == "present"
    assert detail.agent_state_category == "connecting"
