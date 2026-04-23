"""Dokploy-backed Coder runtime backend."""

from __future__ import annotations

import http.client
import json
import re
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib import parse
from urllib import error as urlerror
from urllib import request as urlrequest

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.packs.coder import CoderError, CoderResourceRecord


class DokployCoderApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...
    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...
    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...
    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord: ...
    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokployCoderBackend:
    def __init__(
        self,
        *,
        api_url: str,
        email: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        stack_name: str,
        hostname: str,
        wildcard_hostname: str,
        admin_email: str,
        admin_password: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        client: DokployCoderApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._wildcard_hostname = wildcard_hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._client = client or DokployApiClient(
            api_url=api_url,
            email=email,
            password=password,
            api_key=api_key,
        )
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_service_name(self._stack_name)
        )

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def create_service(self, **kwargs: object) -> CoderResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        wildcard_hostname = str(kwargs["wildcard_hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise CoderError("Coder service name does not match the active Dokploy plan.")
        if hostname != self._hostname or wildcard_hostname != self._wildcard_hostname:
            raise CoderError("Coder hostnames no longer match the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise CoderError("Coder postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def update_service(self, **kwargs: object) -> CoderResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_data_name(self._stack_name)
        )

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(self) -> tuple[str, ...]:
        notes: list[str] = []
        if self._created_in_process:
            _wait_for_coder_bootstrap_api_ready(self._hostname)
        first_user_provisioned = False
        if not _coder_first_user_exists(self._hostname):
            _create_coder_first_user(
                hostname=self._hostname,
                email=self._admin_email,
                password=self._admin_password,
            )
            first_user_provisioned = True
            notes.append(f"Provisioned initial Coder admin for '{self._admin_email}'.")
        session_token = _coder_login(
            hostname=self._hostname,
            email=self._admin_email,
            password=self._admin_password,
        )
        container_name = _coder_container_name(_service_name(self._stack_name))
        if container_name is None:
            raise CoderError("Coder container is not running; cannot finish application bootstrap.")
        _copy_template_into_container(
            container_name=container_name,
            template_dir=_default_template_dir(),
        )
        _push_default_template(
            container_name=container_name,
            hostname=self._hostname,
            session_token=session_token,
            template_name=_default_template_name(),
        )
        notes.append(f"Seeded default Coder template '{_default_template_name()}'.")
        workspace_name = _default_workspace_name(self._hostname)
        try:
            if _ensure_default_workspace(
                container_name=container_name,
                hostname=self._hostname,
                session_token=session_token,
                workspace_name=workspace_name,
                template_name=_default_template_name(),
            ):
                if first_user_provisioned:
                    notes.append(
                        f"Created default Coder workspace '{workspace_name}' for '{self._admin_email}'."
                    )
                else:
                    notes.append(f"Created missing default Coder workspace '{workspace_name}'.")
        except CoderError as e:
            notes.append(f"Skipped default workspace creation: {e}")
        return tuple(notes)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise CoderError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._compose_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            wildcard_hostname=self._wildcard_hostname,
            postgres_service_name=self._postgres_service_name,
            postgres=self._postgres,
        )
        try:
            if self._applied_locator is not None:
                updated = self._client.update_compose(
                    compose_id=self._applied_locator.compose_id, compose_file=compose_file
                )
                self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard coder reconcile",
                    description="Update Coder compose app",
                )
                self._created_in_process = True
                self._applied_locator = _ComposeLocator(
                    project_id=self._applied_locator.project_id,
                    environment_id=self._applied_locator.environment_id,
                    compose_id=updated.compose_id,
                )
                return self._applied_locator
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._compose_name:
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=compose.compose_id,
                        )
                        self._applied_locator = locator
                        return locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file=compose_file,
                    app_name=self._compose_name,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard coder reconcile",
                    description="Create Coder compose app",
                )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._applied_locator = locator
                self._created_in_process = True
                return locator
            created_project = self._client.create_project(
                name=self._stack_name, description="Managed by dokploy-wizard", env=None
            )
            created_compose = self._client.create_compose(
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file=compose_file,
                app_name=self._compose_name,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard coder reconcile",
                description="Create Coder compose app",
            )
        except DokployApiError as error:
            raise CoderError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        self._created_in_process = True
        return locator


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-coder"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-coder-data"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _wildcard_suffix(wildcard_hostname: str) -> str:
    if not wildcard_hostname.startswith("*."):
        raise CoderError("Coder wildcard hostname must start with '*.'")
    return wildcard_hostname.removeprefix("*.")


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    wildcard_hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
) -> str:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    wildcard_suffix = _wildcard_suffix(wildcard_hostname)
    pg_url = (
        f"postgres://{postgres.user_name}:change-me@{postgres_service_name}:5432/"
        f"{postgres.database_name}?sslmode=disable"
    )
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: ghcr.io/coder/coder:latest\n"
        "    restart: unless-stopped\n"
        '    user: "0:0"\n'
        "    environment:\n"
        "      CODER_HTTP_ADDRESS: 0.0.0.0:3000\n"
        f"      CODER_ACCESS_URL: {_yaml_quote(f'https://{hostname}/')}\n"
        f"      CODER_WILDCARD_ACCESS_URL: {_yaml_quote(wildcard_hostname)}\n"
        f"      CODER_PG_CONNECTION_URL: {_yaml_quote(pg_url)}\n"
        '      CODER_DERP_FORCE_WEBSOCKETS: "true"\n'
        f"      CODER_PROXY_TRUSTED_HEADERS: {_yaml_quote('X-Forwarded-For')}\n"
        f"      CODER_PROXY_TRUSTED_ORIGINS: {_yaml_quote('10.0.0.0/8,172.16.0.0/12,192.168.0.0/16')}\n"
        f"      CODER_CACHE_DIRECTORY: {_yaml_quote('/home/coder/.cache')}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.routers.{service_name}-wildcard.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}-wildcard.rule: "HostRegexp(`{{subdomain:.+}}.{wildcard_suffix}`)"\n'
        f'      traefik.http.routers.{service_name}-wildcard.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}-wildcard.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "3000"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'wget -qO- http://127.0.0.1:3000/healthz >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:/home/coder\n"
        "      - /var/run/docker.sock:/var/run/docker.sock\n"
        "    expose:\n"
        "      - '3000'\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
        "volumes:\n"
        f"  {data_name}:\n"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        f"    name: {shared_network}\n"
        "    external: true\n"
    )


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        443,
        timeout=10,
        context=ssl._create_unverified_context(),
    )
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={"Host": parsed.netloc},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def _public_https_health_check(url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with urlrequest.urlopen(url, timeout=10, context=context) as response:  # noqa: S310
            response.read()
            return response.status == 200
    except (urlerror.HTTPError, urlerror.URLError, OSError, TimeoutError):
        return False


def _wait_for_public_https_health(
    url: str, *, attempts: int = 19, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _public_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _wait_for_coder_bootstrap_api_ready(
    hostname: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> None:
    for attempt in range(attempts):
        try:
            _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
            return
        except _CoderHTTPError as exc:
            if exc.status == 404:
                return
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise CoderError(
                f"Coder bootstrap API did not become ready before first-user setup (HTTP {exc.status})."
            ) from exc
    raise CoderError("Coder bootstrap API did not become ready before first-user setup.")


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coder_first_user_exists(hostname: str) -> bool:
    try:
        _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
    except _CoderHTTPError as exc:
        if exc.status == 404:
            return False
        raise CoderError(f"Unable to determine Coder bootstrap state: HTTP {exc.status}") from exc
    return True


def _create_coder_first_user(*, hostname: str, email: str, password: str) -> None:
    _coder_request(
        hostname=hostname,
        method="POST",
        path="/api/v2/users/first",
        payload={
            "email": email,
            "username": _username_from_email(email),
            "name": _display_name_from_email(email),
            "password": password,
        },
        expected_statuses={201},
    )


def _coder_login(*, hostname: str, email: str, password: str) -> str:
    response = _coder_request(
        hostname=hostname,
        method="POST",
        path="/api/v2/users/login",
        payload={"email": email, "password": password},
        expected_statuses={200, 201},
    )
    token = response.get("session_token")
    if not isinstance(token, str) or token == "":
        raise CoderError("Coder login response did not include a session token.")
    return token


def _coder_request(
    *,
    hostname: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    expected_statuses: set[int] | None = None,
) -> dict[str, object]:
    if expected_statuses is None:
        expected_statuses = {200}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        f"https://127.0.0.1{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Host": hostname,
        },
        method=method,
    )
    try:
        context = ssl._create_unverified_context()
        opener = urlrequest.build_opener(urlrequest.HTTPSHandler(context=context))
        with opener.open(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
            if response.status not in expected_statuses:
                raise CoderError(f"Coder request {method} {path} returned HTTP {response.status}.")
            return {} if raw == "" else json.loads(raw)
    except urlerror.HTTPError as exc:
        raise _CoderHTTPError(status=exc.code) from exc


def _coder_container_name(service_name: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={service_name}",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to locate Coder container: {(result.stderr or result.stdout).strip()}"
        )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def _default_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3] / "templates" / "coder" / "default-ubuntu-code-server"
    )


def _default_template_name() -> str:
    return "ubuntu-vscode"


def _default_workspace_name(hostname: str, *, today: date | None = None) -> str:
    root_domain = (
        hostname.split(".", 1)[1] if hostname.startswith("coder.") and "." in hostname else hostname
    )
    root_token = re.sub(r"[^a-z0-9]+", "", root_domain.lower())
    effective_date = (today or date.today()).isoformat()
    suffix = f"-workspace-{effective_date}"
    max_root_length = max(1, 32 - len(suffix))
    normalized_root = (root_token or "workspace")[:max_root_length]
    return f"{normalized_root}{suffix}"


def _copy_template_into_container(*, container_name: str, template_dir: Path) -> None:
    if not template_dir.exists():
        raise CoderError(f"Default Coder template directory is missing: {template_dir}")
    subprocess.run(
        ["docker", "exec", container_name, "rm", "-rf", f"/tmp/{_default_template_name()}"],
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["docker", "cp", str(template_dir), f"{container_name}:/tmp/{_default_template_name()}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to copy default Coder template into container: {(result.stderr or result.stdout).strip()}"
        )


def _push_default_template(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL=https://{hostname}/",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "push",
            template_name,
            "--directory",
            f"/tmp/{template_name}",
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to push default Coder template: {(result.stderr or result.stdout).strip()}"
        )


def _list_workspaces(*, container_name: str, hostname: str, session_token: str) -> tuple[str, ...]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL=https://{hostname}/",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder workspaces: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError("Coder workspace list returned invalid JSON.") from exc
    if not isinstance(payload, list):
        raise CoderError("Coder workspace list returned an unexpected payload shape.")
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _create_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL=https://{hostname}/",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "create",
            workspace_name,
            "--template",
            template_name,
            "--use-parameter-defaults",
            "--yes",
            "--no-wait",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to create default Coder workspace '{workspace_name}': {(result.stderr or result.stdout).strip()}"
        )


def _ensure_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> bool:
    if workspace_name in _list_workspaces(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    ):
        return False
    _create_default_workspace(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        workspace_name=workspace_name,
        template_name=template_name,
    )
    return True


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", local).strip("-")
    return normalized or "admin"


def _display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.title() or "Dokploy Admin"


@dataclass(frozen=True)
class _CoderHTTPError(RuntimeError):
    status: int
