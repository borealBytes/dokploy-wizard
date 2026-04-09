"""Dokploy-backed Matrix runtime backend."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.core.models import PackSharedAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.packs.matrix.models import MatrixResourceRecord
from dokploy_wizard.packs.matrix.reconciler import MatrixError, _http_health_check


class DokployMatrixApi(Protocol):
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


class DokployMatrixBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        shared_allocation: PackSharedAllocation,
        postgres_service_name: str,
        redis_service_name: str,
        secret_refs: tuple[str, ...],
        client: DokployMatrixApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._shared_allocation = shared_allocation
        self._postgres_service_name = postgres_service_name
        self._redis_service_name = redis_service_name
        self._secret_refs = secret_refs
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> MatrixResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return MatrixResourceRecord(
            resource_id=resource_id, resource_name=_service_name(self._stack_name)
        )

    def find_service_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return MatrixResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
        shared_allocation: PackSharedAllocation,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> MatrixResourceRecord:
        if resource_name != _service_name(self._stack_name):
            raise MatrixError("Matrix service name does not match the active Dokploy plan.")
        self._validate_inputs(
            hostname=hostname,
            secret_refs=secret_refs,
            shared_allocation=shared_allocation,
            postgres_service_name=postgres_service_name,
            redis_service_name=redis_service_name,
            data_resource_name=data_resource_name,
        )
        locator = self._ensure_compose_applied()
        return MatrixResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> MatrixResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return MatrixResourceRecord(
            resource_id=resource_id, resource_name=_data_name(self._stack_name)
        )

    def find_persistent_data_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return MatrixResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> MatrixResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise MatrixError("Matrix data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return MatrixResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool:
        if _docker_container_is_up(self._compose_name):
            return True
        if self._compose_status_reached(service.resource_id, allowed_statuses={"done"}):
            if _docker_container_is_up(self._compose_name):
                return True
        return _http_health_check(url)

    def _compose_status_reached(self, resource_id: str, *, allowed_statuses: set[str]) -> bool:
        compose_id = _parse_resource_id(resource_id, "service") or _parse_resource_id(
            resource_id, "data"
        )
        if compose_id is None:
            return False
        for _ in range(30):
            try:
                projects = self._client.list_projects()
            except DokployApiError:
                return False
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    continue
                for compose in environment.composes:
                    if compose.compose_id != compose_id:
                        continue
                    if compose.status is not None and compose.status.lower() in allowed_statuses:
                        return True
            time.sleep(1.0)
        return False

    def _validate_inputs(
        self,
        *,
        hostname: str,
        secret_refs: tuple[str, ...],
        shared_allocation: PackSharedAllocation,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> None:
        if hostname != self._hostname:
            raise MatrixError("Matrix hostname no longer matches the active Dokploy plan.")
        if secret_refs != self._secret_refs:
            raise MatrixError("Matrix secret refs no longer match the active Dokploy plan.")
        if shared_allocation != self._shared_allocation:
            raise MatrixError("Matrix shared allocation no longer matches the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name:
            raise MatrixError("Matrix postgres service binding no longer matches the active plan.")
        if redis_service_name != self._redis_service_name:
            raise MatrixError("Matrix redis service binding no longer matches the active plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise MatrixError("Matrix data resource name no longer matches the active plan.")

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise MatrixError(str(error)) from error
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
            shared_allocation=self._shared_allocation,
            postgres_service_name=self._postgres_service_name,
            redis_service_name=self._redis_service_name,
            secret_refs=self._secret_refs,
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
                        updated = self._client.update_compose(
                            compose_id=compose.compose_id,
                            compose_file=compose_file,
                        )
                        self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title="dokploy-wizard matrix reconcile",
                            description="Update Matrix compose app",
                        )
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=updated.compose_id,
                        )
                        self._created_in_process = True
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
                    title="dokploy-wizard matrix reconcile",
                    description="Create Matrix compose app",
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
                title="dokploy-wizard matrix reconcile",
                description="Create Matrix compose app",
            )
        except DokployApiError as error:
            raise MatrixError(str(error)) from error
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
    return f"{stack_name}-matrix"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-matrix-data"


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:matrix-{kind}"


def _docker_container_is_up(compose_name: str) -> bool:
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
        if compose_name not in name:
            continue
        return status.startswith("Up ")
    return False


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":matrix-{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    shared_allocation: PackSharedAllocation,
    postgres_service_name: str,
    redis_service_name: str,
    secret_refs: tuple[str, ...],
) -> str:
    registration_secret_ref, macaroon_secret_ref = secret_refs
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    postgres = shared_allocation.postgres
    redis = shared_allocation.redis
    if postgres is None or redis is None:
        raise MatrixError(
            "Matrix compose rendering requires postgres and redis shared allocations."
        )
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: matrixdotorg/synapse:latest\n"
        "    restart: unless-stopped\n"
        '    entrypoint: ["/bin/sh", "-c"]\n'
        "    command: >-\n"
        "      if [ ! -f /data/homeserver.yaml ]; then /start.py migrate_config; fi &&\n"
        "      exec /start.py\n"
        "    environment:\n"
        f"      SYNAPSE_SERVER_NAME: {hostname}\n"
        "      SYNAPSE_REPORT_STATS: 'no'\n"
        "      SYNAPSE_NO_TLS: 'yes'\n"
        "      SYNAPSE_CONFIG_PATH: /data/homeserver.yaml\n"
        f"      POSTGRES_HOST: {postgres_service_name}\n"
        f"      POSTGRES_DB: {postgres.database_name}\n"
        f"      POSTGRES_USER: {postgres.user_name}\n"
        f"      POSTGRES_PASSWORD: ${{{postgres.password_secret_ref}:-change-me}}\n"
        f"      REDIS_HOST: {redis_service_name}\n"
        f"      REDIS_PASSWORD: ${{{redis.password_secret_ref}:-change-me}}\n"
        f"      SYNAPSE_REGISTRATION_SHARED_SECRET: ${{{registration_secret_ref}:-change-me}}\n"
        f"      SYNAPSE_MACAROON_SECRET_KEY: ${{{macaroon_secret_ref}:-change-me}}\n"
        "    expose:\n"
        "      - '8008'\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'wget -q -O- "
        "http://127.0.0.1:8008/_matrix/client/versions >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {data_name}:/data\n"
        "volumes:\n"
        f"  {data_name}:\n"
    )
