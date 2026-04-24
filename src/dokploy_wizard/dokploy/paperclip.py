"""Dokploy-backed Paperclip runtime backend."""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from typing import Protocol
from urllib import error as urlerror
from urllib import parse
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
from dokploy_wizard.packs.paperclip import (
    PaperclipBootstrapState,
    PaperclipError,
    PaperclipResourceRecord,
)

_DEFAULT_PAPERCLIP_IMAGE = "ghcr.io/paperclipai/paperclip:latest"
_DEFAULT_PAPERCLIP_PORT = "3100"
_DEFAULT_PAPERCLIP_ENV_DB_PASSWORD = "change-me"
_DEFAULT_PAPERCLIP_ENV_BETTER_AUTH_SECRET = "change-me"


class DokployPaperclipApi(Protocol):
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


class DokployPaperclipBackend:
    def __init__(
        self,
        *,
        api_url: str,
        email: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        stack_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        client: DokployPaperclipApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
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

    def get_service(self, resource_id: str) -> PaperclipResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return PaperclipResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name),
        )

    def find_service_by_name(self, resource_name: str) -> PaperclipResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return PaperclipResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(self, **kwargs: object) -> PaperclipResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        env = _validate_paperclip_env(kwargs["env"])
        if resource_name != _service_name(self._stack_name):
            raise PaperclipError("Paperclip service name does not match the active Dokploy plan.")
        if hostname != self._hostname:
            raise PaperclipError("Paperclip hostname no longer matches the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise PaperclipError(
                "Paperclip postgres inputs no longer match the active Dokploy plan."
            )
        if data_resource_name != _data_name(self._stack_name):
            raise PaperclipError(
                "Paperclip data resource name does not match the active Dokploy plan."
            )
        if env["PAPERCLIP_PUBLIC_URL"] != f"https://{hostname}":
            raise PaperclipError(
                "Paperclip public URL no longer matches the active Dokploy plan."
            )
        locator = self._ensure_compose_applied(env=env)
        return PaperclipResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def update_service(self, **kwargs: object) -> PaperclipResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> PaperclipResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return PaperclipResourceRecord(
            resource_id=resource_id,
            resource_name=_data_name(self._stack_name),
        )

    def find_persistent_data_by_name(self, resource_name: str) -> PaperclipResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return PaperclipResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> PaperclipResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise PaperclipError("Paperclip data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return PaperclipResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: PaperclipResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(
        self, *, better_auth_secret_ref: str
    ) -> PaperclipBootstrapState:
        if better_auth_secret_ref != _better_auth_secret_ref(self._stack_name):
            raise PaperclipError(
                "Paperclip BETTER_AUTH secret ref no longer matches the active Dokploy plan."
            )
        return PaperclipBootstrapState(ready=True)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise PaperclipError(str(error)) from error
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

    def _ensure_compose_applied(self, *, env: dict[str, str] | None = None) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process and env is None:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            env=env or _default_paperclip_env(self._hostname),
        )
        try:
            if self._applied_locator is not None:
                updated = self._client.update_compose(
                    compose_id=self._applied_locator.compose_id,
                    compose_file=compose_file,
                )
                self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard paperclip reconcile",
                    description="Update Paperclip compose app",
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
                        updated = self._client.update_compose(
                            compose_id=compose.compose_id,
                            compose_file=compose_file,
                        )
                        self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title="dokploy-wizard paperclip reconcile",
                            description="Update Paperclip compose app",
                        )
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=updated.compose_id,
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
                    title="dokploy-wizard paperclip reconcile",
                    description="Create Paperclip compose app",
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
                name=self._stack_name,
                description="Managed by dokploy-wizard",
                env=None,
            )
            created_compose = self._client.create_compose(
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file=compose_file,
                app_name=self._compose_name,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard paperclip reconcile",
                description="Create Paperclip compose app",
            )
        except DokployApiError as error:
            raise PaperclipError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._created_in_process = True
        self._applied_locator = locator
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
    return f"{stack_name}-paperclip"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-paperclip-home"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _better_auth_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-paperclip-better-auth-secret"


def _health_url(hostname: str) -> str:
    return f"https://{hostname}/api/health"


def _default_paperclip_env(hostname: str) -> dict[str, str]:
    return {
        "DATABASE_URL": (
            "postgres://paperclip:"
            f"{_DEFAULT_PAPERCLIP_ENV_DB_PASSWORD}"
            "@postgres:5432/paperclip?sslmode=disable"
        ),
        "PAPERCLIP_HOME": "/var/lib/paperclip",
        "BETTER_AUTH_SECRET": _DEFAULT_PAPERCLIP_ENV_BETTER_AUTH_SECRET,
        "PAPERCLIP_PUBLIC_URL": f"https://{hostname}",
    }


def _validate_paperclip_env(env: object) -> dict[str, str]:
    if not isinstance(env, dict):
        raise PaperclipError("Paperclip runtime env must be provided as a dict.")
    normalized = {str(key): str(value) for key, value in env.items()}
    missing = {
        "DATABASE_URL",
        "PAPERCLIP_HOME",
        "BETTER_AUTH_SECRET",
        "PAPERCLIP_PUBLIC_URL",
    } - set(normalized)
    if missing:
        missing_items = ", ".join(sorted(missing))
        raise PaperclipError(f"Paperclip runtime env is missing required keys: {missing_items}.")
    return normalized


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    env: dict[str, str],
) -> str:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    runtime_env = {
        "HOST": "0.0.0.0",
        "PORT": _DEFAULT_PAPERCLIP_PORT,
        "SERVE_UI": "true",
        **env,
        "PAPERCLIP_DEPLOYMENT_MODE": "authenticated",
        "PAPERCLIP_DEPLOYMENT_EXPOSURE": "private",
    }
    forwarded_proto = (
        f"      traefik.http.middlewares.{service_name}-forwarded-https."
        'headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
    )
    forwarded_host = (
        f"      traefik.http.middlewares.{service_name}-forwarded-https."
        f'headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
    )
    forwarded_port = (
        f"      traefik.http.middlewares.{service_name}-forwarded-https."
        'headers.customrequestheaders.X-Forwarded-Port: "443"\n'
    )
    service_port = (
        f"      traefik.http.services.{service_name}.loadbalancer."
        f'server.port: "{_DEFAULT_PAPERCLIP_PORT}"\n'
    )
    return (
        "services:\n"
        f"  {service_name}:\n"
        f"    image: {_DEFAULT_PAPERCLIP_IMAGE}\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"{_yaml_env_block(runtime_env, indent='      ')}"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f"{forwarded_proto}"
        f"{forwarded_host}"
        f"{forwarded_port}"
        f"{service_port}"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'curl -fsS http://127.0.0.1:3100/api/health >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:{env['PAPERCLIP_HOME']}\n"
        "    expose:\n"
        f"      - '{_DEFAULT_PAPERCLIP_PORT}'\n"
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
        f"# Managed health endpoint: {_health_url(hostname)}\n"
    )


def _yaml_env_block(env: dict[str, str], *, indent: str) -> str:
    return "".join(f"{indent}{key}: {_yaml_quote(value)}\n" for key, value in env.items())


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = urlrequest.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urlrequest.urlopen(req, timeout=15, context=context):  # noqa: S310
            return True
    except urlerror.HTTPError:
        return False
    except (urlerror.URLError, TimeoutError):
        return False


def _public_https_health_check(url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with urlrequest.urlopen(url, timeout=10, context=context):  # noqa: S310
            return True
    except (urlerror.HTTPError, urlerror.URLError, OSError, TimeoutError):
        return False


def _wait_for_public_https_health(
    url: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _public_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
