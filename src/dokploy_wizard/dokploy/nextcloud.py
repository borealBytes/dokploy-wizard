"""Dokploy-backed paired Nextcloud + OnlyOffice runtime backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.packs.nextcloud.models import NextcloudResourceRecord
from dokploy_wizard.packs.nextcloud.reconciler import NextcloudError, _http_health_check


class DokployNextcloudApi(Protocol):
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


class DokployNextcloudBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        nextcloud_hostname: str,
        onlyoffice_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        integration_secret_ref: str,
        client: DokployNextcloudApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _nextcloud_service_name(stack_name)
        self._nextcloud_hostname = nextcloud_hostname
        self._onlyoffice_hostname = onlyoffice_hostname
        self._postgres_service_name = postgres_service_name
        self._redis_service_name = redis_service_name
        self._postgres = postgres
        self._redis = redis
        self._integration_secret_ref = integration_secret_ref
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> NextcloudResourceRecord | None:
        parsed = _parse_service_resource_id(resource_id)
        if parsed is None:
            return None
        compose_id, service_kind = parsed
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return NextcloudResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name_for_kind(self._stack_name, service_kind),
        )

    def find_service_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        service_kind = _service_kind_from_name(self._stack_name, resource_name)
        if service_kind is None:
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return NextcloudResourceRecord(
            resource_id=_service_resource_id(locator.compose_id, service_kind),
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        service_kind = _service_kind_from_name(self._stack_name, resource_name)
        if service_kind is None:
            raise NextcloudError(
                f"Nextcloud service name '{resource_name}' does not match the active Dokploy plan."
            )
        self._validate_service_inputs(
            service_kind=service_kind,
            hostname=hostname,
            data_volume_name=data_volume_name,
            config=config,
        )
        locator = self._ensure_compose_applied()
        return NextcloudResourceRecord(
            resource_id=_service_resource_id(locator.compose_id, service_kind),
            resource_name=resource_name,
        )

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None:
        parsed = _parse_volume_resource_id(resource_id)
        if parsed is None:
            return None
        compose_id, volume_kind = parsed
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return NextcloudResourceRecord(
            resource_id=resource_id,
            resource_name=_volume_name_for_kind(self._stack_name, volume_kind),
        )

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        volume_kind = _volume_kind_from_name(self._stack_name, resource_name)
        if volume_kind is None:
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return NextcloudResourceRecord(
            resource_id=_volume_resource_id(locator.compose_id, volume_kind),
            resource_name=resource_name,
        )

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord:
        volume_kind = _volume_kind_from_name(self._stack_name, resource_name)
        if volume_kind is None:
            raise NextcloudError(
                f"Nextcloud volume name '{resource_name}' does not match the active Dokploy plan."
            )
        locator = self._ensure_compose_applied()
        return NextcloudResourceRecord(
            resource_id=_volume_resource_id(locator.compose_id, volume_kind),
            resource_name=resource_name,
        )

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def _validate_service_inputs(
        self,
        *,
        service_kind: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> None:
        if service_kind == "nextcloud":
            if hostname != self._nextcloud_hostname:
                raise NextcloudError(
                    "Nextcloud hostname no longer matches the active Dokploy plan."
                )
            if data_volume_name != _nextcloud_volume_name(self._stack_name):
                raise NextcloudError(
                    "Nextcloud volume name no longer matches the active Dokploy plan."
                )
            if config.get("onlyoffice_url") != f"https://{self._onlyoffice_hostname}":
                raise NextcloudError(
                    "Nextcloud OnlyOffice URL binding no longer matches the active plan."
                )
            if config.get("postgres_database_name") != self._postgres.database_name:
                raise NextcloudError(
                    "Nextcloud postgres binding no longer matches the active plan."
                )
            if config.get("redis_identity_name") != self._redis.identity_name:
                raise NextcloudError("Nextcloud redis binding no longer matches the active plan.")
            return
        if service_kind == "onlyoffice":
            if hostname != self._onlyoffice_hostname:
                raise NextcloudError(
                    "OnlyOffice hostname no longer matches the active Dokploy plan."
                )
            if data_volume_name != _onlyoffice_volume_name(self._stack_name):
                raise NextcloudError(
                    "OnlyOffice volume name no longer matches the active Dokploy plan."
                )
            if config.get("nextcloud_url") != f"https://{self._nextcloud_hostname}":
                raise NextcloudError(
                    "OnlyOffice Nextcloud URL binding no longer matches the active plan."
                )
            if config.get("integration_secret_ref") != self._integration_secret_ref:
                raise NextcloudError(
                    "OnlyOffice JWT integration secret no longer matches the active plan."
                )
            return
        raise NextcloudError(f"Unsupported Nextcloud service kind '{service_kind}'.")

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise NextcloudError(str(error)) from error
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
            nextcloud_hostname=self._nextcloud_hostname,
            onlyoffice_hostname=self._onlyoffice_hostname,
            postgres_service_name=self._postgres_service_name,
            redis_service_name=self._redis_service_name,
            postgres=self._postgres,
            redis=self._redis,
            integration_secret_ref=self._integration_secret_ref,
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
                    title="dokploy-wizard nextcloud reconcile",
                    description="Create Nextcloud + OnlyOffice compose app",
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
                title="dokploy-wizard nextcloud reconcile",
                description="Create Nextcloud + OnlyOffice compose app",
            )
        except DokployApiError as error:
            raise NextcloudError(str(error)) from error
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


def _nextcloud_service_name(stack_name: str) -> str:
    return f"{stack_name}-nextcloud"


def _onlyoffice_service_name(stack_name: str) -> str:
    return f"{stack_name}-onlyoffice"


def _nextcloud_volume_name(stack_name: str) -> str:
    return f"{stack_name}-nextcloud-data"


def _onlyoffice_volume_name(stack_name: str) -> str:
    return f"{stack_name}-onlyoffice-data"


def _service_name_for_kind(stack_name: str, kind: str) -> str:
    if kind == "nextcloud":
        return _nextcloud_service_name(stack_name)
    if kind == "onlyoffice":
        return _onlyoffice_service_name(stack_name)
    raise NextcloudError(f"Unsupported Nextcloud service kind '{kind}'.")


def _volume_name_for_kind(stack_name: str, kind: str) -> str:
    if kind == "nextcloud":
        return _nextcloud_volume_name(stack_name)
    if kind == "onlyoffice":
        return _onlyoffice_volume_name(stack_name)
    raise NextcloudError(f"Unsupported Nextcloud volume kind '{kind}'.")


def _service_kind_from_name(stack_name: str, resource_name: str) -> str | None:
    if resource_name == _nextcloud_service_name(stack_name):
        return "nextcloud"
    if resource_name == _onlyoffice_service_name(stack_name):
        return "onlyoffice"
    return None


def _volume_kind_from_name(stack_name: str, resource_name: str) -> str | None:
    if resource_name == _nextcloud_volume_name(stack_name):
        return "nextcloud"
    if resource_name == _onlyoffice_volume_name(stack_name):
        return "onlyoffice"
    return None


def _service_resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}-service"


def _volume_resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}-volume"


def _parse_service_resource_id(resource_id: str) -> tuple[str, str] | None:
    prefix = "dokploy-compose:"
    if not resource_id.startswith(prefix):
        return None
    parts = resource_id.removeprefix(prefix).split(":", 1)
    if len(parts) != 2:
        return None
    compose_id, kind = parts
    if kind not in {"nextcloud-service", "onlyoffice-service"}:
        return None
    return compose_id, kind.removesuffix("-service")


def _parse_volume_resource_id(resource_id: str) -> tuple[str, str] | None:
    prefix = "dokploy-compose:"
    if not resource_id.startswith(prefix):
        return None
    parts = resource_id.removeprefix(prefix).split(":", 1)
    if len(parts) != 2:
        return None
    compose_id, kind = parts
    if kind not in {"nextcloud-volume", "onlyoffice-volume"}:
        return None
    return compose_id, kind.removesuffix("-volume")


def _render_compose_file(
    *,
    stack_name: str,
    nextcloud_hostname: str,
    onlyoffice_hostname: str,
    postgres_service_name: str,
    redis_service_name: str,
    postgres: SharedPostgresAllocation,
    redis: SharedRedisAllocation,
    integration_secret_ref: str,
) -> str:
    nextcloud_service = _nextcloud_service_name(stack_name)
    onlyoffice_service = _onlyoffice_service_name(stack_name)
    nextcloud_volume = _nextcloud_volume_name(stack_name)
    onlyoffice_volume = _onlyoffice_volume_name(stack_name)
    nextcloud_url = f"https://{nextcloud_hostname}"
    onlyoffice_url = f"https://{onlyoffice_hostname}"
    return (
        "services:\n"
        f"  {nextcloud_service}:\n"
        "    image: nextcloud:apache\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      POSTGRES_HOST: {postgres_service_name}\n"
        f"      POSTGRES_DB: {postgres.database_name}\n"
        f"      POSTGRES_USER: {postgres.user_name}\n"
        f"      POSTGRES_PASSWORD: ${{{postgres.password_secret_ref}:-change-me}}\n"
        f"      REDIS_HOST: {redis_service_name}\n"
        f"      REDIS_HOST_PASSWORD: ${{{redis.password_secret_ref}:-change-me}}\n"
        f"      NEXTCLOUD_TRUSTED_DOMAINS: {nextcloud_hostname}\n"
        f"      OVERWRITEHOST: {nextcloud_hostname}\n"
        "      OVERWRITEPROTOCOL: https\n"
        f"      ONLYOFFICE_URL: {onlyoffice_url}\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'php -f /var/www/html/status.php >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 10s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {nextcloud_volume}:/var/www/html\n"
        "    expose:\n"
        "      - '80'\n"
        "    depends_on:\n"
        f"      - {onlyoffice_service}\n"
        f"  {onlyoffice_service}:\n"
        "    image: onlyoffice/documentserver:latest\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        "      JWT_ENABLED: 'true'\n"
        f"      JWT_SECRET: ${{{integration_secret_ref}:-change-me}}\n"
        f"      NEXTCLOUD_URL: {nextcloud_url}\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'curl -fsS http://localhost/info/info.json >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {onlyoffice_volume}:/var/lib/onlyoffice\n"
        "    expose:\n"
        "      - '80'\n"
        "volumes:\n"
        f"  {nextcloud_volume}:\n"
        f"  {onlyoffice_volume}:\n"
    )
