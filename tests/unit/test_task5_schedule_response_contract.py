from __future__ import annotations

from typing import Literal, TypeAlias, assert_never
from urllib import request

import pytest

from dokploy_wizard.dokploy.client import DokployApiClient, DokployApiError

Operation: TypeAlias = Literal["list", "create", "update"]


def _invoke(client: DokployApiClient, operation: Operation) -> None:
    match operation:
        case "list":
            client.list_compose_schedules(compose_id="cmp-1")
        case "create":
            client.create_schedule(
                name="sync",
                compose_id="cmp-1",
                service_name="litellm",
                cron_expression="0 3 * * *",
                timezone="UTC",
                shell_type="bash",
                command="true",
                enabled=True,
            )
        case "update":
            client.update_schedule(
                schedule_id="sch-1",
                name="sync",
                compose_id="cmp-1",
                service_name="litellm",
                cron_expression="0 3 * * *",
                timezone="UTC",
                shell_type="bash",
                command="true",
                enabled=True,
            )
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("operation", ["list", "create", "update"])
@pytest.mark.parametrize("missing_field", ["composeId", "scheduleType"])
def test_schedule_response_rejects_missing_remote_identity(
    operation: Operation,
    missing_field: str,
) -> None:
    payload = {
        "scheduleId": "sch-1",
        "name": "sync",
        "serviceName": "litellm",
        "cronExpression": "0 3 * * *",
        "timezone": "UTC",
        "shellType": "bash",
        "command": "true",
        "enabled": True,
        "composeId": "cmp-1",
        "scheduleType": "compose",
    }
    del payload[missing_field]

    def fake_request(_: request.Request) -> object:
        return {"data": [payload] if operation == "list" else payload}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="key",
        request_fn=fake_request,
    )

    with pytest.raises(DokployApiError, match=missing_field):
        _invoke(client, operation)


@pytest.mark.parametrize("operation", ["list", "create", "update"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("composeId", "foreign-compose"), ("scheduleType", "service")],
)
def test_schedule_response_rejects_mismatched_remote_identity(
    operation: Operation,
    field: str,
    value: str,
) -> None:
    payload = {
        "scheduleId": "sch-1",
        "name": "sync",
        "serviceName": "litellm",
        "cronExpression": "0 3 * * *",
        "timezone": "UTC",
        "shellType": "bash",
        "command": "true",
        "enabled": True,
        "composeId": "cmp-1",
        "scheduleType": "compose",
        field: value,
    }

    def fake_request(_: request.Request) -> object:
        return {"data": [payload] if operation == "list" else payload}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="key",
        request_fn=fake_request,
    )

    with pytest.raises(DokployApiError, match=field):
        _invoke(client, operation)
