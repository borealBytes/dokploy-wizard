"""Dokploy-backed shared-core backend using a compose-first deployment flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.core import SharedCoreError, SharedCorePlan, SharedCoreResourceRecord
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)


class DokploySharedCoreApi(Protocol):
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


class DokploySharedCoreBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        plan: SharedCorePlan,
        client: DokploySharedCoreApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._plan = plan
        self._compose_name = plan.network_name
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self._lookup_locator(resource_id, "network") is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=resource_id, resource_name=self._plan.network_name
        )

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if resource_name != self._plan.network_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "network"),
            resource_name=resource_name,
        )

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        if resource_name != self._plan.network_name:
            raise SharedCoreError("Shared-core network name does not match the active plan.")
        locator = self._ensure_compose_applied()
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "network"),
            resource_name=resource_name,
        )

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self._plan.postgres is None or self._lookup_locator(resource_id, "postgres") is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=resource_id,
            resource_name=self._plan.postgres.service_name,
        )

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self._plan.postgres is None or resource_name != self._plan.postgres.service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "postgres"),
            resource_name=resource_name,
        )

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        if self._plan.postgres is None or resource_name != self._plan.postgres.service_name:
            raise SharedCoreError("Shared-core Postgres name does not match the active plan.")
        locator = self._ensure_compose_applied()
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "postgres"),
            resource_name=resource_name,
        )

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self._plan.redis is None or self._lookup_locator(resource_id, "redis") is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=resource_id,
            resource_name=self._plan.redis.service_name,
        )

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self._plan.redis is None or resource_name != self._plan.redis.service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "redis"),
            resource_name=resource_name,
        )

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        if self._plan.redis is None or resource_name != self._plan.redis.service_name:
            raise SharedCoreError("Shared-core Redis name does not match the active plan.")
        locator = self._ensure_compose_applied()
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "redis"),
            resource_name=resource_name,
        )

    def _lookup_locator(self, resource_id: str, kind: str) -> _ComposeLocator | None:
        compose_id = _parse_resource_id(resource_id, kind)
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
            raise SharedCoreError(str(error)) from error
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
                            compose_file=_render_compose_file(self._plan),
                        )
                        self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title="dokploy-wizard shared core reconcile",
                            description="Update shared core compose app",
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
                    compose_file=_render_compose_file(self._plan),
                    app_name=self._compose_name,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard shared core reconcile",
                    description="Create shared core compose app",
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
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file=_render_compose_file(self._plan),
                app_name=self._compose_name,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard shared core reconcile",
                description="Create shared core compose app",
            )
        except DokployApiError as error:
            raise SharedCoreError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        return locator


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _render_compose_file(plan: SharedCorePlan) -> str:
    postgres_block = ""
    volume_block = ""
    if plan.postgres is not None:
        postgres_volume = f"{plan.postgres.service_name}-data"
        postgres_block = (
            f"  {plan.postgres.service_name}:\n"
            "    image: postgres:16-alpine\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      POSTGRES_DB: postgres\n"
            "      POSTGRES_USER: postgres\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change-me}\n"
            f"    volumes:\n      - {postgres_volume}:/var/lib/postgresql/data\n"
            "    networks:\n      - shared\n"
        )
        volume_block += f"  {postgres_volume}:\n"
    redis_block = ""
    if plan.redis is not None:
        redis_volume = f"{plan.redis.service_name}-data"
        redis_block = (
            f"  {plan.redis.service_name}:\n"
            "    image: redis:7-alpine\n"
            "    restart: unless-stopped\n"
            "    command: redis-server --appendonly yes "
            "--requirepass ${REDIS_PASSWORD:-change-me}\n"
            f"    volumes:\n      - {redis_volume}:/data\n"
            "    networks:\n      - shared\n"
        )
        volume_block += f"  {redis_volume}:\n"
    return (
        "services:\n"
        f"{postgres_block}"
        f"{redis_block}"
        "networks:\n"
        "  shared:\n"
        f"    name: {plan.network_name}\n"
        "volumes:\n"
        f"{volume_block or '  {}\n'}"
    )
