"""Dokploy-backed SeaweedFS runtime backend."""

from __future__ import annotations

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
from dokploy_wizard.packs.seaweedfs import SeaweedFsError, SeaweedFsResourceRecord


class DokploySeaweedFsApi(Protocol):
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


class DokploySeaweedFsBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        client: DokploySeaweedFsApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return SeaweedFsResourceRecord(
            resource_id=resource_id, resource_name=_service_name(self._stack_name)
        )

    def find_service_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SeaweedFsResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        if resource_name != _service_name(self._stack_name):
            raise SeaweedFsError("SeaweedFS service name does not match the active Dokploy plan.")
        if (
            hostname != self._hostname
            or access_key != self._access_key
            or secret_key != self._secret_key
        ):
            raise SeaweedFsError(
                "SeaweedFS service inputs no longer match the active Dokploy plan."
            )
        if data_resource_name != _data_name(self._stack_name):
            raise SeaweedFsError(
                "SeaweedFS data resource name no longer matches the active Dokploy plan."
            )
        locator = self._ensure_compose_applied()
        return SeaweedFsResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return SeaweedFsResourceRecord(
            resource_id=resource_id, resource_name=_data_name(self._stack_name)
        )

    def find_persistent_data_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SeaweedFsResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> SeaweedFsResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise SeaweedFsError("SeaweedFS data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return SeaweedFsResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: SeaweedFsResourceRecord, url: str) -> bool:
        del service
        from dokploy_wizard.packs.seaweedfs.reconciler import _http_health_check

        return _http_health_check(url)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise SeaweedFsError(str(error)) from error
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
        if self._applied_locator is not None:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        try:
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
                    title="dokploy-wizard seaweedfs reconcile",
                    description="Create SeaweedFS compose app",
                )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._created_in_process = True
                self._applied_locator = locator
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
                title="dokploy-wizard seaweedfs reconcile",
                description="Create SeaweedFS compose app",
            )
        except DokployApiError as error:
            raise SeaweedFsError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._created_in_process = True
        self._applied_locator = locator
        return locator


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs-data"


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:seaweedfs-{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":seaweedfs-{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _render_compose_file(
    *, stack_name: str, hostname: str, access_key: str, secret_key: str
) -> str:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: chrislusf/seaweedfs:latest\n"
        "    restart: unless-stopped\n"
        "    command: ['weed', 'server', '-dir=/data', '-s3']\n"
        "    environment:\n"
        f"      AWS_ACCESS_KEY_ID: {access_key}\n"
        f"      AWS_SECRET_ACCESS_KEY: {secret_key}\n"
        f"      S3_DOMAIN_NAME: {hostname}\n"
        "    expose:\n"
        "      - '8333'\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'wget -q -O- http://127.0.0.1:8333/status >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {data_name}:/data\n"
        "volumes:\n"
        f"  {data_name}:\n"
    )
