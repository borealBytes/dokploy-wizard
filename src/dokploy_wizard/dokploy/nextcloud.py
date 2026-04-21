"""Dokploy-backed paired Nextcloud + OnlyOffice runtime backend."""

from __future__ import annotations

import json
import re
import shlex
import ssl
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib import error, parse, request

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
    DokployScheduleRecord,
)
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudOpenClawWorkspaceContract,
    NextcloudResourceRecord,
    TalkRuntime,
)
from dokploy_wizard.packs.nextcloud.reconciler import NextcloudError

_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_DEFAULT_NEXTCLOUD_ADMIN_USER = "admin"
_DEFAULT_NEXTCLOUD_ADMIN_PASSWORD = "ChangeMeSoon"
_DEFAULT_SHARED_SERVICE_PASSWORD = "change-me"
_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME = "/OpenClaw"
_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_PATH = "/mnt/openclaw"
_DEFAULT_OPENCLAW_RESCAN_CRON = "*/15 * * * *"
_DEFAULT_OPENCLAW_RESCAN_TIMEZONE = "UTC"
_DEFAULT_ONLYOFFICE_DEF_FORMATS = {
    "docx": True,
    "xlsx": True,
    "pptx": True,
    "pdf": True,
    "docxf": True,
    "oform": True,
    "vsdx": True,
}
_DEFAULT_ONLYOFFICE_EDIT_FORMATS = {
    "csv": True,
    "txt": True,
}
_ONLYOFFICE_DOCUMENTSERVER_CHECK_ATTEMPTS = 180
_ONLYOFFICE_DOCUMENTSERVER_CHECK_DELAY_SECONDS = 5.0
_NEXTCLOUD_APPSTORE_APPS_JSON_URL = "https://apps.nextcloud.com/api/v1/apps.json"


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

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]: ...

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord: ...

    def update_schedule(
        self,
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord: ...

    def delete_schedule(self, *, schedule_id: str) -> None: ...


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
        admin_user: str = _DEFAULT_NEXTCLOUD_ADMIN_USER,
        admin_password: str = _DEFAULT_NEXTCLOUD_ADMIN_PASSWORD,
        openclaw_volume_name: str | None = None,
        openclaw_workspace_contract: NextcloudOpenClawWorkspaceContract | None = None,
        nexa_agent_user_id: str | None = None,
        nexa_agent_display_name: str | None = None,
        nexa_agent_password: str | None = None,
        nexa_agent_email: str | None = None,
        openclaw_rescan_cron: str = _DEFAULT_OPENCLAW_RESCAN_CRON,
        openclaw_rescan_timezone: str = _DEFAULT_OPENCLAW_RESCAN_TIMEZONE,
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
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._openclaw_volume_name = openclaw_volume_name
        self._openclaw_workspace_contract = openclaw_workspace_contract
        self._nexa_agent_user_id = nexa_agent_user_id
        self._nexa_agent_display_name = nexa_agent_display_name
        self._nexa_agent_password = nexa_agent_password
        self._nexa_agent_email = nexa_agent_email
        self._openclaw_rescan_cron = openclaw_rescan_cron
        self._openclaw_rescan_timezone = openclaw_rescan_timezone
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

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            data_volume_name=data_volume_name,
            config=config,
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
        if service.resource_name == _nextcloud_service_name(self._stack_name):
            if not _nextcloud_status_ready(url):
                return False
            if self._admin_user != _DEFAULT_NEXTCLOUD_ADMIN_USER:
                container = _find_container_name(service.resource_name)
                if container is None:
                    return False
                return _nextcloud_user_exists(container, self._admin_user)
            return True
        if service.resource_name == _onlyoffice_service_name(self._stack_name):
            if _local_https_health_check(url):
                return True
            if _public_https_health_check(url):
                return True
            if self._created_in_process:
                return _wait_for_public_https_health(url)
            return False
        return _local_https_health_check(url)

    def ensure_application_ready(
        self, *, nextcloud_url: str, onlyoffice_url: str
    ) -> NextcloudBundleVerification:
        document_server_url = _with_trailing_slash(onlyoffice_url)
        document_server_internal_url = _with_trailing_slash(
            f"http://{_onlyoffice_service_name(self._stack_name)}"
        )
        storage_url = _with_trailing_slash(f"http://{_nextcloud_service_name(self._stack_name)}")
        if _nextcloud_status_ready(f"{nextcloud_url}/status.php"):
            container = _find_container_name(_nextcloud_service_name(self._stack_name))
            if container is None:
                raise NextcloudError(
                    "Nextcloud container could not be located for OnlyOffice and Talk verification."
                )
            _ensure_admin_user(container, self._admin_user, self._admin_password)
            _ensure_nexa_service_account(
                container,
                user_id=self._nexa_agent_user_id,
                password=self._nexa_agent_password,
                display_name=self._nexa_agent_display_name,
                email=self._nexa_agent_email,
            )
            _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
            _ensure_onlyoffice_app_config(
                container,
                document_server_url=document_server_url,
                document_server_internal_url=document_server_internal_url,
                storage_url=storage_url,
                jwt_secret=_DEFAULT_SHARED_SERVICE_PASSWORD,
                wait_for_documentserver_check=self._created_in_process,
                openclaw_external_storage_enabled=self._openclaw_volume_name is not None,
                admin_user=self._admin_user,
            )
            self._ensure_openclaw_rescan_schedule()
            return _verify_nextcloud_bundle(container)
        container = _wait_for_container_name(_nextcloud_service_name(self._stack_name))
        if container is None:
            raise NextcloudError(
                "Nextcloud container is not running; cannot finish application bootstrap."
            )
        if not _wait_for_nextcloud_status_ready(f"{nextcloud_url}/status.php"):
            raise NextcloudError(
                "Nextcloud did not finish its container bootstrap before application "
                "configuration was attempted."
            )
        _ensure_admin_user(container, self._admin_user, self._admin_password)
        _ensure_nexa_service_account(
            container,
            user_id=self._nexa_agent_user_id,
            password=self._nexa_agent_password,
            display_name=self._nexa_agent_display_name,
            email=self._nexa_agent_email,
        )
        _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
        _ensure_onlyoffice_app_config(
            container,
            document_server_url=document_server_url,
            document_server_internal_url=document_server_internal_url,
            storage_url=storage_url,
            jwt_secret=_DEFAULT_SHARED_SERVICE_PASSWORD,
            wait_for_documentserver_check=self._created_in_process,
            openclaw_external_storage_enabled=self._openclaw_volume_name is not None,
            admin_user=self._admin_user,
        )
        self._ensure_openclaw_rescan_schedule()
        return _verify_nextcloud_bundle(container)

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        if self._openclaw_volume_name is None:
            return
        container = _find_container_name(_nextcloud_service_name(self._stack_name))
        if container is None:
            raise NextcloudError(
                "Nextcloud container could not be located for OpenClaw external storage refresh."
            )
        _ensure_files_external_app(container)
        _ensure_openclaw_external_storage(container, admin_user=admin_user)
        self._ensure_openclaw_rescan_schedule()

    def _ensure_openclaw_rescan_schedule(self) -> None:
        if self._openclaw_volume_name is None:
            return
        locator = self._find_compose_locator()
        if locator is None:
            raise NextcloudError(
                "Nextcloud compose locator is unavailable for schedule reconciliation."
            )
        schedule_name = f"{self._stack_name}-openclaw-rescan"
        service_name = _nextcloud_service_name(self._stack_name)
        command = f'php /var/www/html/occ files:scan --path="{self._admin_user}/files{_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME}"'
        existing = next(
            (
                item
                for item in self._client.list_compose_schedules(compose_id=locator.compose_id)
                if item.name == schedule_name
            ),
            None,
        )
        if existing is None:
            self._client.create_schedule(
                name=schedule_name,
                compose_id=locator.compose_id,
                service_name=service_name,
                cron_expression=self._openclaw_rescan_cron,
                timezone=self._openclaw_rescan_timezone,
                shell_type="bash",
                command=command,
                enabled=True,
            )
            return
        if (
            existing.service_name != service_name
            or existing.cron_expression != self._openclaw_rescan_cron
            or existing.timezone != self._openclaw_rescan_timezone
            or existing.shell_type != "bash"
            or existing.command != command
            or existing.enabled is not True
        ):
            self._client.update_schedule(
                schedule_id=existing.schedule_id,
                name=schedule_name,
                compose_id=locator.compose_id,
                service_name=service_name,
                cron_expression=self._openclaw_rescan_cron,
                timezone=self._openclaw_rescan_timezone,
                shell_type="bash",
                command=command,
                enabled=True,
            )

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
        if self._applied_locator is not None and self._created_in_process:
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
            admin_user=self._admin_user,
            admin_password=self._admin_password,
            openclaw_volume_name=self._openclaw_volume_name,
            openclaw_workspace_contract=self._openclaw_workspace_contract,
        )
        try:
            if self._applied_locator is not None:
                updated = self._client.update_compose(
                    compose_id=self._applied_locator.compose_id,
                    compose_file=compose_file,
                )
                self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard nextcloud reconcile",
                    description="Update Nextcloud + OnlyOffice compose app",
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
                            title="dokploy-wizard nextcloud reconcile",
                            description="Update Nextcloud + OnlyOffice compose app",
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


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _nextcloud_volume_name(stack_name: str) -> str:
    return f"{stack_name}-nextcloud-data"


def _onlyoffice_volume_name(stack_name: str) -> str:
    return f"{stack_name}-onlyoffice-data"


def _openclaw_volume_name(stack_name: str) -> str:
    return f"{stack_name}-openclaw-data"


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
    admin_user: str,
    admin_password: str,
    openclaw_volume_name: str | None,
    openclaw_workspace_contract: NextcloudOpenClawWorkspaceContract | None,
) -> str:
    nextcloud_service = _nextcloud_service_name(stack_name)
    onlyoffice_service = _onlyoffice_service_name(stack_name)
    nextcloud_volume = _nextcloud_volume_name(stack_name)
    onlyoffice_volume = _onlyoffice_volume_name(stack_name)
    openclaw_volume = openclaw_volume_name
    shared_network = _shared_network_name(stack_name)
    nextcloud_url = f"https://{nextcloud_hostname}"
    onlyoffice_url = f"https://{onlyoffice_hostname}"
    nextcloud_workspace_env = ""
    if openclaw_workspace_contract is not None:
        nextcloud_workspace_env = (
            f"      DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_STORAGE_MODE: operator-surface\n"
            f"      DOKPLOY_WIZARD_OPENCLAW_NEXA_VISIBLE_ROOT: {openclaw_workspace_contract.visible_root}\n"
            f"      DOKPLOY_WIZARD_OPENCLAW_NEXA_CONTRACT_PATH: {openclaw_workspace_contract.contract_path}\n"
            "      DOKPLOY_WIZARD_OPENCLAW_NEXA_RUNTIME_STATE_SOURCE: "
            f"{openclaw_workspace_contract.runtime_state_source}\n"
        )
    nextcloud_extra_mount = (
        f"      - {openclaw_volume}:{_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_PATH}\n"
        if openclaw_volume is not None
        else ""
    )
    openclaw_volume_block = (
        f"  {openclaw_volume}:\n    name: {openclaw_volume}\n"
        if openclaw_volume is not None
        else ""
    )
    return (
        "services:\n"
        f"  {nextcloud_service}:\n"
        "    image: nextcloud:apache\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      POSTGRES_HOST: {postgres_service_name}\n"
        f"      POSTGRES_DB: {postgres.database_name}\n"
        f"      POSTGRES_USER: {postgres.user_name}\n"
        f"      POSTGRES_PASSWORD: {_DEFAULT_SHARED_SERVICE_PASSWORD}\n"
        f"      REDIS_HOST: {redis_service_name}\n"
        f"      REDIS_HOST_PASSWORD: {_DEFAULT_SHARED_SERVICE_PASSWORD}\n"
        f"      NEXTCLOUD_ADMIN_USER: {admin_user}\n"
        f"      NEXTCLOUD_ADMIN_PASSWORD: {admin_password}\n"
        f"      NEXTCLOUD_TRUSTED_DOMAINS: {nextcloud_hostname}\n"
        f"      TRUSTED_PROXIES: {_DEFAULT_TRUSTED_PROXIES}\n"
        f"      OVERWRITEHOST: {nextcloud_hostname}\n"
        "      OVERWRITEPROTOCOL: https\n"
        f"      OVERWRITECLIURL: {nextcloud_url}\n"
        f"      ONLYOFFICE_URL: {onlyoffice_url}\n"
        f"{nextcloud_workspace_env}"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{nextcloud_service}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{nextcloud_service}.rule: "Host(`{nextcloud_hostname}`)"\n'
        f'      traefik.http.routers.{nextcloud_service}.tls: "true"\n'
        f'      traefik.http.services.{nextcloud_service}.loadbalancer.server.port: "80"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'php -f /var/www/html/status.php >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 10s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {nextcloud_volume}:/var/www/html\n"
        f"{nextcloud_extra_mount}"
        "    expose:\n"
        "      - '80'\n"
        "    networks:\n"
        "      default:\n"
        "      dokploy-network:\n"
        f"      {shared_network}:\n"
        "        aliases:\n"
        f"          - {nextcloud_service}\n"
        "    depends_on:\n"
        f"      - {onlyoffice_service}\n"
        f"  {onlyoffice_service}:\n"
        "    image: onlyoffice/documentserver:latest\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        "      JWT_ENABLED: 'true'\n"
        f"      JWT_SECRET: {_DEFAULT_SHARED_SERVICE_PASSWORD}\n"
        "      JWT_HEADER: Authorization\n"
        "      ALLOW_PRIVATE_IP_ADDRESS: 'true'\n"
        "      ALLOW_META_IP_ADDRESS: 'true'\n"
        f"      NEXTCLOUD_URL: {nextcloud_url}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{onlyoffice_service}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{onlyoffice_service}.rule: "Host(`{onlyoffice_hostname}`)"\n'
        f'      traefik.http.routers.{onlyoffice_service}.middlewares: "{onlyoffice_service}-forwarded-https"\n'
        f'      traefik.http.routers.{onlyoffice_service}.tls: "true"\n'
        f'      traefik.http.middlewares.{onlyoffice_service}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{onlyoffice_service}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{onlyoffice_hostname}"\n'
        f'      traefik.http.middlewares.{onlyoffice_service}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{onlyoffice_service}.loadbalancer.server.port: "80"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'curl -fsS http://127.0.0.1/healthcheck >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
        f"    volumes:\n      - {onlyoffice_volume}:/var/lib/onlyoffice\n"
        "    expose:\n"
        "      - '80'\n"
        "    networks:\n"
        "      default:\n"
        "      dokploy-network:\n"
        f"      {shared_network}:\n"
        "        aliases:\n"
        f"          - {onlyoffice_service}\n"
        "volumes:\n"
        f"  {nextcloud_volume}:\n"
        f"  {onlyoffice_volume}:\n"
        f"{openclaw_volume_block}"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        "    external: true\n"
    )


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = request.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with request.urlopen(req, timeout=15, context=context):  # noqa: S310
            return True
    except error.HTTPError as exc:
        return exc.code < 500
    except (error.URLError, TimeoutError):
        return False


def _public_https_health_check(url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with request.urlopen(url, timeout=10, context=context):  # noqa: S310
            return True
    except (error.HTTPError, error.URLError, OSError, TimeoutError):
        return False


def _wait_for_public_https_health(
    url: str, *, attempts: int = 10, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _public_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _nextcloud_status_ready(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/status.php"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = request.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with request.urlopen(req, timeout=15, context=context) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
        return False
    return bool(payload.get("installed")) and not bool(payload.get("maintenance"))


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def _wait_for_nextcloud_status_ready(
    url: str, *, attempts: int = 60, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _nextcloud_status_ready(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _find_container_name(service_name: str) -> str | None:
    try:
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
    except OSError:
        return None
    if result.returncode != 0:
        return None
    container_names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not container_names:
        return None
    if service_name in container_names:
        return service_name
    if len(container_names) == 1:
        return container_names[0]
    preferred_suffix = f"-{service_name}-1"
    for container_name in container_names:
        if container_name.endswith(preferred_suffix):
            return container_name
    return sorted(container_names)[0]


def _wait_for_container_name(
    service_name: str, *, attempts: int = 60, delay_seconds: float = 5.0
) -> str | None:
    for attempt in range(attempts):
        container_name = _find_container_name(service_name)
        if container_name is not None:
            return container_name
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _run_occ(container_name: str, args: list[str]) -> None:
    _run_occ_command(_occ_command(container_name, args), args)


def _read_occ_www_data_output(container_name: str, args: list[str]) -> str:
    result = subprocess.run(
        _occ_command(container_name, args),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise NextcloudError(
            f"Nextcloud OCC command failed ({' '.join(args)}): {detail or 'unknown error'}"
        )
    return result.stdout


def _occ_command(container_name: str, args: list[str]) -> list[str]:
    return [
        "docker",
        "exec",
        container_name,
        "su",
        "-s",
        "/bin/sh",
        "www-data",
        "-c",
        "cd /var/www/html && php occ " + " ".join(shlex.quote(arg) for arg in args),
    ]


def _run_occ_shell(container_name: str, shell_command: str) -> None:
    command = [
        "docker",
        "exec",
        container_name,
        "su",
        "-s",
        "/bin/sh",
        "www-data",
        "-c",
        f"cd /var/www/html && {shell_command}",
    ]
    _run_occ_command(command, [shell_command])


def _run_occ_command(command: list[str], display_args: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise NextcloudError(
            f"Nextcloud OCC command failed ({' '.join(display_args)}): {detail or 'unknown error'}"
        )


def _ensure_admin_user(container_name: str, admin_user: str, admin_password: str) -> None:
    if admin_user == _DEFAULT_NEXTCLOUD_ADMIN_USER:
        return
    exists = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "su",
            "-s",
            "/bin/sh",
            "www-data",
            "-c",
            f"cd /var/www/html && php occ user:info {shlex.quote(admin_user)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if exists.returncode == 0:
        return
    add_command = (
        f"export OC_PASS={shlex.quote(admin_password)} && "
        f"php occ user:add --password-from-env --group admin {shlex.quote(admin_user)} && "
        f"php occ user:setting {shlex.quote(admin_user)} settings email {shlex.quote(admin_user)}"
    )
    _run_occ_shell(container_name, add_command)


def _ensure_nexa_service_account(
    container_name: str,
    *,
    user_id: str | None,
    password: str | None,
    display_name: str | None,
    email: str | None,
) -> None:
    if user_id is None or password is None:
        return
    safe_user = shlex.quote(user_id)
    exists = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "su",
            "-s",
            "/bin/sh",
            "www-data",
            "-c",
            f"cd /var/www/html && php occ user:info {safe_user}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        add_command = (
            f"export OC_PASS={shlex.quote(password)} && "
            f"php occ user:add --password-from-env --display-name={shlex.quote(display_name or user_id)} {safe_user}"
        )
        _run_occ_shell(container_name, add_command)
    if display_name is not None:
        _run_occ_shell_allow_noop(
            container_name,
            f"php occ user:setting {safe_user} settings display_name {shlex.quote(display_name)}",
            noop_fragments=("same",),
        )
    if email is not None:
        _run_occ_shell_allow_noop(
            container_name,
            f"php occ user:setting {safe_user} settings email {shlex.quote(email)}",
            noop_fragments=("same",),
        )
    _run_occ_shell_allow_noop(
        container_name,
        f"php occ user:profile {safe_user} profile_enabled 1",
        noop_fragments=("same",),
    )


def _run_occ_shell_allow_noop(
    container_name: str,
    shell_command: str,
    *,
    noop_fragments: tuple[str, ...],
) -> None:
    try:
        _run_occ_shell(container_name, shell_command)
    except NextcloudError as error:
        detail = str(error).lower()
        if any(fragment in detail for fragment in noop_fragments):
            return
        raise


def _ensure_onlyoffice_app_config(
    container_name: str,
    document_server_url: str,
    document_server_internal_url: str,
    storage_url: str,
    jwt_secret: str,
    wait_for_documentserver_check: bool = False,
    openclaw_external_storage_enabled: bool = False,
    admin_user: str = _DEFAULT_NEXTCLOUD_ADMIN_USER,
) -> None:
    def_formats = json.dumps(_DEFAULT_ONLYOFFICE_DEF_FORMATS, separators=(",", ":"), sort_keys=True)
    edit_formats = json.dumps(
        _DEFAULT_ONLYOFFICE_EDIT_FORMATS,
        separators=(",", ":"),
        sort_keys=True,
    )
    _enable_app_with_release_fallback(
        container_name,
        enable_command="php occ app:enable --force onlyoffice",
        install_from_release=_install_onlyoffice_app_from_release,
    )
    _run_occ_shell(
        container_name,
        "php occ config:system:set allow_local_remote_servers --value=true --type=bool",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:system:set onlyoffice jwt_secret --value={shlex.quote(jwt_secret)}",
    )
    _run_occ_shell(
        container_name,
        "php occ config:system:set onlyoffice jwt_header --value=Authorization",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set onlyoffice DocumentServerUrl --value={shlex.quote(document_server_url)}",
    )
    _run_occ_shell(
        container_name,
        "php occ config:app:set onlyoffice DocumentServerInternalUrl "
        f"--value={shlex.quote(document_server_internal_url)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set onlyoffice StorageUrl --value={shlex.quote(storage_url)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set onlyoffice jwt_secret --value={shlex.quote(jwt_secret)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set onlyoffice defFormats --value={shlex.quote(def_formats)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set onlyoffice editFormats --value={shlex.quote(edit_formats)}",
    )
    _run_occ_shell(container_name, "php occ config:app:set onlyoffice sameTab --value=true")
    _run_occ_shell(container_name, "php occ config:app:set onlyoffice preview --value=true")
    if wait_for_documentserver_check:
        _wait_for_onlyoffice_documentserver_check(container_name)
    else:
        _run_occ_shell(container_name, "php occ onlyoffice:documentserver --check")
    if openclaw_external_storage_enabled:
        _ensure_files_external_app(container_name)
        _ensure_openclaw_external_storage(container_name, admin_user=admin_user)


def _wait_for_onlyoffice_documentserver_check(
    container_name: str,
    *,
    attempts: int = _ONLYOFFICE_DOCUMENTSERVER_CHECK_ATTEMPTS,
    delay_seconds: float = _ONLYOFFICE_DOCUMENTSERVER_CHECK_DELAY_SECONDS,
) -> None:
    for attempt in range(attempts):
        try:
            _run_occ_shell(container_name, "php occ onlyoffice:documentserver --check")
            return
        except NextcloudError:
            if attempt >= attempts - 1:
                raise
            time.sleep(delay_seconds)


def _verify_nextcloud_bundle(container_name: str) -> NextcloudBundleVerification:
    _ensure_spreed_app_enabled(container_name)
    enabled = _talk_app_enabled(container_name)
    return NextcloudBundleVerification(
        onlyoffice_document_server_check=_command_check(
            container_name,
            command="php occ onlyoffice:documentserver --check",
        ),
        talk=TalkRuntime(
            app_id="spreed",
            enabled=enabled,
            enabled_check=NextcloudCommandCheck(
                command="php occ app:list --output=json",
                passed=enabled,
            ),
            signaling_check=_command_check(
                container_name,
                command="php occ talk:signaling:list --output=json",
            ),
            stun_check=_command_check(
                container_name,
                command="php occ talk:stun:list --output=json",
            ),
            turn_check=_command_check(
                container_name,
                command="php occ talk:turn:list --output=json",
            ),
        ),
    )


def _ensure_files_external_app(container_name: str) -> None:
    _run_occ(container_name, ["app:enable", "files_external"])


def _list_external_storage_mounts(container_name: str) -> tuple[dict[str, object], ...]:
    output = _read_occ_www_data_output(
        container_name, ["files_external:list", "--output=json"]
    ).strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise NextcloudError(
            "Nextcloud external storage list did not return valid JSON."
        ) from error
    if not isinstance(payload, list):
        raise NextcloudError(
            "Nextcloud external storage list returned an unexpected payload shape."
        )
    return tuple(item for item in payload if isinstance(item, dict))


def _find_external_storage_mount_id(
    container_name: str, *, mount_point: str, datadir: str
) -> str | None:
    for mount in _list_external_storage_mounts(container_name):
        mount_name = mount.get("mount_point") or mount.get("mountPoint") or mount.get("mount")
        config = mount.get("configuration") or mount.get("config")
        mount_id = mount.get("mount_id") or mount.get("mountId") or mount.get("id")
        config_datadir = config.get("datadir") if isinstance(config, dict) else None
        if mount_name == mount_point or config_datadir == datadir:
            if mount_id is not None:
                return str(mount_id)
    return None


def _ensure_openclaw_external_storage(container_name: str, *, admin_user: str) -> None:
    mount_id = _find_external_storage_mount_id(
        container_name,
        mount_point=_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME,
        datadir=_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_PATH,
    )
    if mount_id is None:
        _run_occ(
            container_name,
            [
                "files_external:create",
                _DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME,
                "local",
                "null::null",
                "-c",
                f"datadir={_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_PATH}",
            ],
        )
        mount_id = _find_external_storage_mount_id(
            container_name,
            mount_point=_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME,
            datadir=_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_PATH,
        )
        if mount_id is None:
            raise NextcloudError("Nextcloud external storage mount for OpenClaw was not created.")
    _run_occ(container_name, ["files_external:applicable", mount_id, f"--add-user={admin_user}"])
    _run_occ(container_name, ["files_external:option", mount_id, "readonly", "false"])
    _run_occ(container_name, ["files_external:verify", mount_id])
    _run_occ(container_name, ["files_external:scan", mount_id])
    _run_occ(
        container_name,
        ["files:scan", f"--path={admin_user}/files{_DEFAULT_OPENCLAW_EXTERNAL_MOUNT_NAME}"],
    )


def _ensure_spreed_app_enabled(container_name: str) -> None:
    _enable_app_with_release_fallback(
        container_name,
        enable_command="php occ app:enable spreed",
        install_from_release=_install_spreed_app_from_release,
    )


def _enable_app_with_release_fallback(
    container_name: str,
    *,
    enable_command: str,
    install_from_release: Callable[[str], None],
) -> None:
    try:
        _run_occ_shell(container_name, enable_command)
    except NextcloudError:
        install_from_release(container_name)
        _run_occ_shell(container_name, enable_command)


def _install_onlyoffice_app_from_release(container_name: str) -> None:
    _install_nextcloud_app_from_release(container_name, app_id="onlyoffice", app_label="ONLYOFFICE")


def _install_spreed_app_from_release(container_name: str) -> None:
    _install_nextcloud_app_from_release(container_name, app_id="spreed", app_label="Talk")


def _install_nextcloud_app_from_release(
    container_name: str, *, app_id: str, app_label: str
) -> None:
    download_url = _resolve_compatible_app_release_download_url(container_name, app_id)
    _run_occ_shell(
        container_name,
        'export NEXTCLOUD_APP_TMP_DIR="$(mktemp -d)" && '
        "trap 'rm -rf \"$NEXTCLOUD_APP_TMP_DIR\"' EXIT && "
        f'php -r \'if (!copy("{download_url}", getenv("NEXTCLOUD_APP_TMP_DIR") . "/app-release.tar.gz")) {{ fwrite(STDERR, "Failed to download {app_label} app release\\n"); exit(1); }}\' && '
        f"rm -rf apps/{app_id} && "
        'tar -xzf "$NEXTCLOUD_APP_TMP_DIR/app-release.tar.gz" -C apps && '
        f"test -d apps/{app_id}",
    )


def _resolve_compatible_app_release_download_url(container_name: str, app_id: str) -> str:
    nextcloud_major = _read_installed_nextcloud_major_version(container_name)
    apps = _fetch_nextcloud_appstore_apps()
    for app in apps:
        if app.get("id") != app_id:
            continue
        releases = app.get("releases")
        if not isinstance(releases, list):
            break
        for release in releases:
            if not isinstance(release, dict):
                continue
            download = release.get("download")
            platform_spec = release.get("platformVersionSpec")
            if not isinstance(download, str) or download == "":
                continue
            if not isinstance(platform_spec, str) or platform_spec == "":
                continue
            if _platform_version_spec_matches_major(platform_spec, nextcloud_major):
                return download
        break
    raise NextcloudError(
        f"Nextcloud appstore did not provide a compatible signed download URL for '{app_id}' on Nextcloud {nextcloud_major}."
    )


def _read_installed_nextcloud_major_version(container_name: str) -> int:
    output = _read_occ_www_data_output(container_name, ["status", "--output=json"]).strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise NextcloudError("Nextcloud status did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise NextcloudError("Nextcloud status did not return a JSON object.")
    version_string = payload.get("versionstring")
    if not isinstance(version_string, str) or version_string == "":
        raise NextcloudError("Nextcloud status did not include a versionstring.")
    major = _parse_version_major(version_string)
    if major is None:
        raise NextcloudError("Nextcloud status did not include a parseable major version.")
    return major


def _fetch_nextcloud_appstore_apps() -> tuple[dict[str, object], ...]:
    req = request.Request(_NEXTCLOUD_APPSTORE_APPS_JSON_URL, method="GET")
    try:
        with request.urlopen(req, timeout=15) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NextcloudError("Nextcloud appstore metadata could not be fetched.") from exc
    apps = payload.get("apps") if isinstance(payload, dict) else payload
    if not isinstance(apps, list):
        raise NextcloudError("Nextcloud appstore metadata did not return an app list.")
    parsed_apps: list[dict[str, object]] = []
    for item in apps:
        if isinstance(item, dict):
            parsed_apps.append(item)
    return tuple(parsed_apps)


def _platform_version_spec_matches_major(platform_spec: str, nextcloud_major: int) -> bool:
    normalized = platform_spec.replace(",", " ")
    clauses = [clause.strip() for clause in normalized.split() if clause.strip() != ""]
    if not clauses:
        return False
    return all(
        _platform_version_clause_matches_major(clause, nextcloud_major) for clause in clauses
    )


def _platform_version_clause_matches_major(clause: str, nextcloud_major: int) -> bool:
    for operator in (">=", "<=", ">", "<", "==", "="):
        if not clause.startswith(operator):
            continue
        version_major = _parse_version_major(clause[len(operator) :].strip())
        if version_major is None:
            return False
        if operator == ">=":
            return nextcloud_major >= version_major
        if operator == "<=":
            return nextcloud_major <= version_major
        if operator == ">":
            return nextcloud_major > version_major
        if operator == "<":
            return nextcloud_major < version_major
        return nextcloud_major == version_major
    version_major = _parse_version_major(clause)
    return version_major is not None and nextcloud_major == version_major


def _parse_version_major(value: str) -> int | None:
    major_text = value.strip().split(".", 1)[0]
    if major_text.isdigit():
        return int(major_text)
    return None


def _talk_app_enabled(container_name: str) -> bool:
    output = _read_occ_www_data_output(container_name, ["app:list", "--output=json"]).strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise NextcloudError("Nextcloud app:list did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise NextcloudError("Nextcloud app:list did not return a JSON object.")
    enabled = payload.get("enabled")
    if isinstance(enabled, list):
        return "spreed" in enabled
    if isinstance(enabled, dict):
        return "spreed" in enabled
    raise NextcloudError("Nextcloud app:list did not include an enabled app collection.")


def _command_check(container_name: str, *, command: str) -> NextcloudCommandCheck:
    _run_occ_shell(container_name, command)
    return NextcloudCommandCheck(command=command, passed=True)


def _nextcloud_user_exists(container_name: str, admin_user: str) -> bool:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "su",
            "-s",
            "/bin/sh",
            "www-data",
            "-c",
            f"cd /var/www/html && php occ user:info {shlex.quote(admin_user)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _ensure_trusted_domain(container_name: str, hostname: str) -> None:
    existing = _read_occ_output(
        container_name, ["php", "occ", "config:system:get", "trusted_domains"]
    )
    current_domains = tuple(line.strip() for line in existing.splitlines() if line.strip())
    if hostname in current_domains:
        return
    _run_occ_shell(
        container_name,
        f"php occ config:system:set trusted_domains {len(current_domains)} --value={shlex.quote(hostname)}",
    )


def _read_occ_output(container_name: str, args: list[str]) -> str:
    command = ["docker", "exec", container_name, *args]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise NextcloudError(
            f"Nextcloud OCC command failed ({' '.join(args)}): {detail or 'unknown error'}"
        )
    return result.stdout
