"""Dokploy-backed Headscale runtime backend."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.packs.headscale.models import HeadscaleResourceRecord
from dokploy_wizard.packs.headscale.reconciler import HeadscaleError, _http_health_check


class DokployHeadscaleApi(Protocol):
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


HealthCheckFn = Protocol


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokployHeadscaleBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        client: DokployHeadscaleApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._hostname = hostname
        self._service_name = f"{stack_name}-headscale"
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        locator = self._lookup_locator(resource_id)
        if locator is None:
            return None
        return HeadscaleResourceRecord(resource_id=resource_id, resource_name=self._service_name)

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
        if resource_name != self._service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return HeadscaleResourceRecord(
            resource_id=_resource_id(locator.compose_id),
            resource_name=self._service_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        if resource_name != self._service_name:
            raise HeadscaleError("Headscale service name does not match the active Dokploy plan.")
        self._hostname = hostname
        locator = self._ensure_compose_applied(secret_refs=secret_refs)
        return HeadscaleResourceRecord(
            resource_id=_resource_id(locator.compose_id),
            resource_name=self._service_name,
        )

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service
        return _wait_for_headscale_health(self._service_name, url)

    def _lookup_locator(self, resource_id: str) -> _ComposeLocator | None:
        compose_id = _parse_resource_id(resource_id)
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return locator

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise HeadscaleError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._service_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self, *, secret_refs: tuple[str, ...]) -> _ComposeLocator:
        try:
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._service_name:
                        updated = self._client.update_compose(
                            compose_id=compose.compose_id,
                            compose_file=_render_compose_file(
                                self._service_name, self._hostname, secret_refs
                            ),
                        )
                        self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title="dokploy-wizard headscale reconcile",
                            description="Update Headscale compose app",
                        )
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=updated.compose_id,
                        )
                        self._applied_locator = locator
                        return locator

                created = self._client.create_compose(
                    name=self._service_name,
                    environment_id=environment.environment_id,
                    compose_file=_render_compose_file(
                        self._service_name, self._hostname, secret_refs
                    ),
                    app_name=self._service_name,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard headscale reconcile",
                    description="Create Headscale compose app",
                )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._applied_locator = locator
                return locator

            created_project = self._client.create_project(
                name=self._stack_name,
                description="Managed by dokploy-wizard",
                env=None,
            )
            created_compose = self._client.create_compose(
                name=self._service_name,
                environment_id=created_project.environment_id,
                compose_file=_render_compose_file(self._service_name, self._hostname, secret_refs),
                app_name=self._service_name,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard headscale reconcile",
                description="Create Headscale compose app",
            )
        except DokployApiError as error:
            raise HeadscaleError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        return locator


def _resource_id(compose_id: str) -> str:
    return f"dokploy-compose:{compose_id}:headscale"


def _docker_container_is_up(service_name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if service_name not in name:
            continue
        return status.startswith("Up ")
    return False


def _parse_resource_id(resource_id: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = ":headscale"
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


def _render_compose_file(service_name: str, hostname: str, secret_refs: tuple[str, ...]) -> str:
    admin_secret_ref, noise_secret_ref = secret_refs
    volume_name = f"{service_name}-data"
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: headscale/headscale:latest\n"
        "    restart: unless-stopped\n"
        "    command: ['serve']\n"
        "    environment:\n"
        f"      HEADSCALE_SERVER_URL: https://{hostname}\n"
        "      HEADSCALE_LISTEN_ADDR: 0.0.0.0:8080\n"
        "      HEADSCALE_METRICS_LISTEN_ADDR: 0.0.0.0:9090\n"
        "      HEADSCALE_NOISE_PRIVATE_KEY_PATH: /var/lib/headscale/noise_private.key\n"
        "      HEADSCALE_PREFIXES_V4: 100.64.0.0/10\n"
        "      HEADSCALE_PREFIXES_ALLOCATION: sequential\n"
        "      HEADSCALE_DERP_URLS: https://controlplane.tailscale.com/derpmap/default\n"
        "      HEADSCALE_DISABLE_CHECK_UPDATES: 'true'\n"
        "      HEADSCALE_DATABASE_TYPE: sqlite\n"
        "      HEADSCALE_DATABASE_SQLITE_PATH: /var/lib/headscale/db.sqlite\n"
        "      HEADSCALE_UNIX_SOCKET: /var/run/headscale/headscale.sock\n"
        "      HEADSCALE_LOG_FORMAT: text\n"
        "      HEADSCALE_LOG_LEVEL: info\n"
        "      HEADSCALE_DNS_OVERRIDE_LOCAL_DNS: 'false'\n"
        "      HEADSCALE_DNS_MAGIC_DNS: 'false'\n"
        f"      HEADSCALE_ADMIN_API_KEY: ${{{admin_secret_ref}:-change-me}}\n"
        f"      HEADSCALE_NOISE_PRIVATE_KEY: ${{{noise_secret_ref}:-change-me}}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "8080"\n'
        "    expose:\n"
        "      - '8080'\n"
        f"    volumes:\n      - {volume_name}:/var/lib/headscale\n"
        "      - /var/run/headscale:/var/run/headscale\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        "volumes:\n"
        f"  {volume_name}:\n"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
    )


def _wait_for_headscale_health(
    service_name: str,
    url: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 5.0,
) -> bool:
    for attempt in range(attempts):
        if _docker_container_is_up(service_name):
            return True
        if _http_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False
