# pyright: reportMissingImports=false

from __future__ import annotations

import http.cookiejar
import json
from email.message import Message
from io import BytesIO
from typing import cast
from urllib import error, request

import pytest

from dokploy_wizard.dokploy import DokployApiClient, DokployApiError


def test_dokploy_client_logs_in_with_email_password_and_reuses_session_cookie() -> None:
    requests_seen: list[str] = []

    def fake_request(req: request.Request, jar: http.cookiejar.CookieJar) -> object:
        requests_seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            jar.set_cookie(_session_cookie())
            return {"user": {"email": "admin@example.com"}}
        assert any(cookie.name == "better-auth.session_token" for cookie in jar)
        assert "X-api-key" not in dict(req.header_items())
        return {"data": [{"projectId": "proj-1", "name": "wizard", "environments": []}]}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/",
        email="admin@example.com",
        password="secret-123",
        api_key="legacy-key-123",
        request_fn=fake_request,
    )

    first = client.list_projects()
    second = client.list_projects()

    assert first[0].project_id == "proj-1"
    assert second[0].project_id == "proj-1"
    assert requests_seen == [
        "https://dokploy.example.com/api/auth/sign-in/email",
        "https://dokploy.example.com/api/project.all",
        "https://dokploy.example.com/api/project.all",
    ]


def test_dokploy_client_falls_back_to_api_key_when_session_endpoint_is_unavailable() -> None:
    requests_seen: list[str] = []

    def fake_request(req: request.Request) -> object:
        requests_seen.append(req.full_url)
        if req.full_url.endswith("/api/auth/sign-in/email"):
            raise error.HTTPError(
                req.full_url,
                404,
                "Not Found",
                hdrs=Message(),
                fp=BytesIO(b'{"message":"Not Found"}'),
            )
        headers = dict(req.header_items())
        assert headers["X-api-key"] == "dokp-key-123"
        return {"data": [{"projectId": "proj-1", "name": "wizard", "environments": []}]}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/",
        email="admin@example.com",
        password="secret-123",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    projects = client.list_projects()

    assert projects[0].project_id == "proj-1"
    assert requests_seen == [
        "https://dokploy.example.com/api/auth/sign-in/email",
        "https://dokploy.example.com/api/project.all",
    ]


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
    requests_seen: list[tuple[str, dict[str, object]]] = []

    def fake_request(req: request.Request) -> object:
        body = cast(bytes | None, req.data)
        payload = json.loads(body.decode("utf-8")) if body is not None else {}
        requests_seen.append((req.full_url, payload))
        if req.full_url.endswith("/api/compose.create"):
            return {"data": {"composeId": "cmp-1", "name": "wizard-shared"}}
        if req.full_url.endswith("/api/compose.update"):
            return {"data": {"composeId": "cmp-1", "name": "wizard-shared"}}
        raise AssertionError(req.full_url)

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

    assert requests_seen[0][0] == "https://dokploy.example.com/api/compose.create"
    assert requests_seen[0][1] == {
        "name": "wizard-shared",
        "environmentId": "env-1",
        "composeType": "docker-compose",
        "appName": "wizard-shared",
    }
    assert requests_seen[1][0] == "https://dokploy.example.com/api/compose.update"
    body = requests_seen[1][1]
    assert body["composeId"] == "cmp-1"
    assert body["composePath"] == "./docker-compose.yml"
    assert body["sourceType"] == "raw"
    assert body["githubId"] is None
    assert body["repository"] is None
    assert body["owner"] is None
    assert body["branch"] is None
    assert record.compose_id == "cmp-1"


def test_dokploy_client_deletes_project_with_json_payload() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        captured["url"] = req.full_url
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {"projectId": "proj-1", "name": "wizard-probe"}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    client.delete_project(project_id="proj-1")

    assert captured["url"] == "https://dokploy.example.com/api/project.remove"
    assert json.loads(str(captured["body"])) == {"projectId": "proj-1"}


def test_dokploy_client_resets_compose_path_when_updating_raw_compose() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        captured["url"] = req.full_url
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {"data": {"composeId": "cmp-1", "name": "wizard-matrix"}}

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    record = client.update_compose(
        compose_id="cmp-1",
        compose_file="services:\n  app:\n    image: ghcr.io/example/app:latest\n",
    )

    body = json.loads(str(captured["body"]))
    assert captured["url"] == "https://dokploy.example.com/api/compose.update"
    assert body["composePath"] == "./docker-compose.yml"
    assert body["sourceType"] == "raw"
    assert body["githubId"] is None
    assert body["repository"] is None
    assert body["owner"] is None
    assert body["branch"] is None
    assert record.compose_id == "cmp-1"


def test_dokploy_client_coerces_null_project_env_to_empty_string() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {
            "data": {
                "project": {"projectId": "proj-1"},
                "environment": {"environmentId": "env-1"},
            }
        }

    client = DokployApiClient(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    created = client.create_project(name="wizard", description="Managed", env=None)

    body = json.loads(str(captured["body"]))
    assert body["env"] == ""
    assert created.project_id == "proj-1"


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


def test_dokploy_client_list_projects_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        request_fn=fake_request,
        list_projects_session_fallback=lambda: [
            {"projectId": "proj-1", "name": "wizard", "environments": []}
        ],
    )

    projects = client.list_projects()

    assert projects[0].project_id == "proj-1"


def test_dokploy_client_list_projects_re_raises_when_fallback_also_fails() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        request_fn=fake_request,
        list_projects_session_fallback=lambda: (_ for _ in ()).throw(
            DokployApiError(
                'Dokploy API request failed with status 401: {"message":"Unauthorized"}.'
            )
        ),
    )

    with pytest.raises(DokployApiError, match="status 401"):
        client.list_projects()


def test_dokploy_client_create_project_uses_project_create_endpoint() -> None:
    captured: dict[str, object] = {}

    def fake_request(req: request.Request) -> object:
        captured["url"] = req.full_url
        body = cast(bytes | None, req.data)
        captured["body"] = body.decode("utf-8") if body is not None else None
        return {
            "data": {
                "project": {"projectId": "proj-1"},
                "environment": {"environmentId": "env-1"},
            }
        }

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    created = client.create_project(name="wizard-probe", description="probe", env="")

    assert captured["url"] == "https://dokploy.example.com/api/project.create"
    body = json.loads(str(captured["body"]))
    assert body["name"] == "wizard-probe"
    assert created.project_id == "proj-1"
    assert created.environment_id == "env-1"


def test_dokploy_client_deploy_compose_uses_session_fallback_on_api_key_401() -> None:
    requests_seen: list[str] = []

    def fake_request(req: request.Request) -> object:
        requests_seen.append(req.full_url)
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        deploy_session_fallback=lambda compose_id, title, description: {
            "data": {
                "success": True,
                "composeId": compose_id,
                "message": f"{title}:{description}",
            }
        },
    )

    result = client.deploy_compose(
        compose_id="cmp-1",
        title="probe",
        description="session-fallback",
    )

    assert requests_seen == ["https://dokploy.example.com/api/compose.deploy"]
    assert result.success is True
    assert result.compose_id == "cmp-1"
    assert result.message == "probe:session-fallback"


def test_dokploy_client_deploy_compose_re_raises_when_fallback_also_fails() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        deploy_session_fallback=lambda compose_id, title, description: (_ for _ in ()).throw(
            DokployApiError(
                'Dokploy API request failed with status 401: {"message":"Unauthorized"}.'
            )
        ),
    )

    with pytest.raises(DokployApiError, match="status 401"):
        client.deploy_compose(compose_id="cmp-1", title="probe", description="fallback-fails")


def test_dokploy_client_create_project_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        project_create_session_fallback=lambda name, description, env: {
            "data": {
                "project": {"projectId": "proj-1"},
                "environment": {"environmentId": "env-1"},
            }
        },
    )

    created = client.create_project(name="wizard-probe", description="probe", env="")

    assert created.project_id == "proj-1"
    assert created.environment_id == "env-1"


def test_dokploy_client_create_compose_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        compose_create_session_fallback=lambda name, environment_id, compose_file, app_name: {
            "data": {"composeId": "cmp-1", "name": name}
        },
        compose_update_session_fallback=lambda compose_id, compose_file: {
            "data": {"composeId": compose_id, "name": "wizard-compose"}
        },
    )

    record = client.create_compose(
        name="wizard-compose",
        environment_id="env-1",
        compose_file="services:{}",
        app_name="wizard-compose",
    )

    assert record.compose_id == "cmp-1"
    assert record.name == "wizard-compose"


def test_dokploy_client_update_compose_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        compose_update_session_fallback=lambda compose_id, compose_file: {
            "data": {"composeId": compose_id, "name": "wizard-compose"}
        },
    )

    record = client.update_compose(compose_id="cmp-1", compose_file="services:{}")

    assert record.compose_id == "cmp-1"
    assert record.name == "wizard-compose"


def test_dokploy_client_list_compose_schedules_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        list_compose_schedules_session_fallback=lambda compose_id: {
            "data": [
                {
                    "scheduleId": "sch-1",
                    "name": "wizard-stack-openclaw-rescan",
                    "serviceName": "wizard-stack-nextcloud",
                    "cronExpression": "*/15 * * * *",
                    "timezone": "UTC",
                    "shellType": "bash",
                    "command": 'php /var/www/html/occ files:scan --path="admin/files/OpenClaw"',
                    "enabled": True,
                }
            ]
        },
    )

    schedules = client.list_compose_schedules(compose_id="cmp-1")

    assert len(schedules) == 1
    assert schedules[0].schedule_id == "sch-1"


def test_dokploy_client_create_schedule_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        create_schedule_session_fallback=lambda *args: {
            "data": {
                "scheduleId": "sch-1",
                "name": args[0],
                "serviceName": args[2],
                "cronExpression": args[3],
                "timezone": args[4],
                "shellType": args[5],
                "command": args[6],
                "enabled": args[7],
            }
        },
    )

    schedule = client.create_schedule(
        name="wizard-stack-openclaw-rescan",
        compose_id="cmp-1",
        service_name="wizard-stack-nextcloud",
        cron_expression="*/15 * * * *",
        timezone="UTC",
        shell_type="bash",
        command='php /var/www/html/occ files:scan --path="admin/files/OpenClaw"',
        enabled=True,
    )

    assert schedule.schedule_id == "sch-1"
    assert schedule.enabled is True


def test_dokploy_client_update_schedule_uses_session_fallback_on_api_key_401() -> None:
    def fake_request(req: request.Request) -> object:
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"Unauthorized"}'),
        )

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
        update_schedule_session_fallback=lambda *args: {
            "data": {
                "scheduleId": args[0],
                "name": args[1],
                "serviceName": args[3],
                "cronExpression": args[4],
                "timezone": args[5],
                "shellType": args[6],
                "command": args[7],
                "enabled": args[8],
            }
        },
    )

    schedule = client.update_schedule(
        schedule_id="sch-1",
        name="wizard-stack-openclaw-rescan",
        compose_id="cmp-1",
        service_name="wizard-stack-nextcloud",
        cron_expression="*/15 * * * *",
        timezone="UTC",
        shell_type="bash",
        command='php /var/www/html/occ files:scan --path="admin/files/OpenClaw"',
        enabled=True,
    )

    assert schedule.schedule_id == "sch-1"
    assert schedule.service_name == "wizard-stack-nextcloud"


def _session_cookie() -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name="better-auth.session_token",
        value="session-token-123",
        port=None,
        port_specified=False,
        domain="dokploy.example.com",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""},
        rfc2109=False,
    )
