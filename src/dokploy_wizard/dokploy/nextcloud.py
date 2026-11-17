# ruff: noqa: E501
"""Dokploy-backed paired Nextcloud + OnlyOffice runtime backend."""

from __future__ import annotations

import json
import shlex
import ssl
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Protocol, cast
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
)
from dokploy_wizard.dokploy.compose_noop import (
    load_compose_artifact_hash,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.env_spec import (
    DokployEnvReconciler,
    DokployEnvSpec,
    DokployEnvVar,
    RenderedCompose,
)
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudAiIntegration,
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudResourceRecord,
    TalkRuntime,
)
from dokploy_wizard.packs.nextcloud.reconciler import NextcloudError
from dokploy_wizard.state import ComposeArtifactHashState, load_state_dir
from dokploy_wizard.verification import ServiceVerificationResult

_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_DEFAULT_NEXTCLOUD_ADMIN_USER = "admin"
_DEFAULT_NEXTCLOUD_ADMIN_PASSWORD = "ChangeMeSoon"
_DEFAULT_SHARED_SERVICE_PASSWORD = "change-me"
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
_NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_ATTEMPTS = 240
_NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_DELAY_SECONDS = 5.0
_NEXTCLOUD_FIRST_BOOT_STATUS_WAIT_ATTEMPTS = 60
_NEXTCLOUD_FIRST_BOOT_STATUS_WAIT_DELAY_SECONDS = 5.0
_NEXTCLOUD_APPSTORE_APPS_JSON_URL = "https://apps.nextcloud.com/api/v1/apps.json"
_INTEGRATION_OPENAI_APP_ID = "integration_openai"
_INTEGRATION_OPENAI_APP_LABEL = "OpenAI/LocalAI integration"
_LITELLM_MODELS_PROBE_ATTEMPTS = 10
_LITELLM_MODELS_PROBE_DELAY_SECONDS = 3.0


class DokployNextcloudApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...

    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...

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
        admin_user: str = _DEFAULT_NEXTCLOUD_ADMIN_USER,
        admin_password: str = _DEFAULT_NEXTCLOUD_ADMIN_PASSWORD,
        state_dir: Path = Path(".dokploy-wizard-state"),
        client: DokployNextcloudApi | None = None,
        ai_integration: NextcloudAiIntegration | None = None,
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
        self._onlyoffice_jwt_secret = _nextcloud_generated_secret(integration_secret_ref)
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._state_dir = state_dir
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._ai_integration = ai_integration
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
            _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
            _ensure_onlyoffice_app_config(
                container,
                document_server_url=document_server_url,
                document_server_internal_url=document_server_internal_url,
                storage_url=storage_url,
                jwt_secret=self._onlyoffice_jwt_secret,
                wait_for_documentserver_check=self._created_in_process,
            )
            self._ensure_ai_integration(container)
            return _verify_nextcloud_bundle(container)
        service_name = _nextcloud_service_name(self._stack_name)
        container = _wait_for_nextcloud_first_boot_ready(
            service_name,
            f"{nextcloud_url}/status.php",
            attempts=_NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_ATTEMPTS,
            delay_seconds=_NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_DELAY_SECONDS,
        )
        if container is None:
            container = _find_container_name(service_name)
            if container is None:
                raise NextcloudError(
                    "Nextcloud container is not running; cannot finish application bootstrap."
                )
            if not _container_health_ready(container):
                raise NextcloudError(
                    "Nextcloud container did not become healthy before application "
                    "configuration was attempted."
                )
            raise NextcloudError(
                "Nextcloud did not finish its container bootstrap before application "
                "configuration was attempted."
            )
        _ensure_admin_user(container, self._admin_user, self._admin_password)
        _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
        _ensure_onlyoffice_app_config(
            container,
            document_server_url=document_server_url,
            document_server_internal_url=document_server_internal_url,
            storage_url=storage_url,
            jwt_secret=self._onlyoffice_jwt_secret,
            wait_for_documentserver_check=self._created_in_process,
        )
        self._ensure_ai_integration(container)
        return _verify_nextcloud_bundle(container)

    def _ensure_ai_integration(self, container: str) -> None:
        if self._ai_integration is None:
            return
        _ensure_ai_integration_openai(container, self._ai_integration)

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
        can_use_stateful_noop_guard = self._can_use_stateful_noop_guard()
        rendered_compose = _render_compose_file(
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
        )
        try:
            if self._applied_locator is not None:
                if can_use_stateful_noop_guard:
                    applied_locator = self._applied_locator
                    result = _apply_rendered_compose_noop_guard(
                        rendered_compose=rendered_compose,
                        service_key=self._compose_name,
                        state_dir=self._state_dir,
                        client=self._client,
                        locator=applied_locator,
                        compose_id=applied_locator.compose_id,
                        title="dokploy-wizard nextcloud reconcile",
                        description="Update Nextcloud + OnlyOffice compose app",
                        verify_current=self._verify_current_application,
                        locator_factory=lambda compose_id: _ComposeLocator(
                            project_id=applied_locator.project_id,
                            environment_id=applied_locator.environment_id,
                            compose_id=compose_id,
                        ),
                    )
                    self._created_in_process = result.status == "applied"
                    self._applied_locator = result.locator
                else:
                    updated = _apply_rendered_compose_to_existing(
                        client=self._client,
                        compose_id=self._applied_locator.compose_id,
                        rendered_compose=rendered_compose,
                    )
                    deployment = self._client.deploy_compose(
                        compose_id=updated.compose_id,
                        title="dokploy-wizard nextcloud reconcile",
                        description="Update Nextcloud + OnlyOffice compose app",
                    )
                    if not deployment.success:
                        raise NextcloudError(
                            "Dokploy deploy for Nextcloud + OnlyOffice compose app did not report success."
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
                        if can_use_stateful_noop_guard:
                            locator = _ComposeLocator(
                                project_id=project.project_id,
                                environment_id=environment.environment_id,
                                compose_id=compose.compose_id,
                            )
                            result = _apply_rendered_compose_noop_guard(
                                rendered_compose=rendered_compose,
                                service_key=self._compose_name,
                                state_dir=self._state_dir,
                                client=self._client,
                                locator=locator,
                                compose_id=compose.compose_id,
                                title="dokploy-wizard nextcloud reconcile",
                                description="Update Nextcloud + OnlyOffice compose app",
                                verify_current=self._verify_current_application,
                                locator_factory=lambda compose_id: _ComposeLocator(
                                    project_id=project.project_id,
                                    environment_id=environment.environment_id,
                                    compose_id=compose_id,
                                ),
                            )
                            self._created_in_process = result.status == "applied"
                            self._applied_locator = result.locator
                            return result.locator
                        updated = _apply_rendered_compose_to_existing(
                            client=self._client,
                            compose_id=compose.compose_id,
                            rendered_compose=rendered_compose,
                        )
                        deployment = self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title="dokploy-wizard nextcloud reconcile",
                            description="Update Nextcloud + OnlyOffice compose app",
                        )
                        if not deployment.success:
                            raise NextcloudError(
                                "Dokploy deploy for Nextcloud + OnlyOffice compose app did not report success."
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
                    compose_file="services: {}\n",
                    app_name=self._compose_name,
                )
                updated = _apply_rendered_compose_to_existing(
                    client=self._client,
                    compose_id=created.compose_id,
                    rendered_compose=rendered_compose,
                )
                deployment = self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard nextcloud reconcile",
                    description="Create Nextcloud + OnlyOffice compose app",
                )
                if not deployment.success:
                    raise NextcloudError(
                        "Dokploy deploy for Nextcloud + OnlyOffice compose app did not report success."
                    )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=updated.compose_id,
                )
                if can_use_stateful_noop_guard:
                    persist_compose_artifact_hash(
                        state_dir=self._state_dir,
                        service_key=self._compose_name,
                        rendered_compose=rendered_compose,
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
                compose_file="services: {}\n",
                app_name=self._compose_name,
            )
            updated_compose = _apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=created_compose.compose_id,
                rendered_compose=rendered_compose,
            )
            deployment = self._client.deploy_compose(
                compose_id=updated_compose.compose_id,
                title="dokploy-wizard nextcloud reconcile",
                description="Create Nextcloud + OnlyOffice compose app",
            )
            if not deployment.success:
                raise NextcloudError(
                    "Dokploy deploy for Nextcloud + OnlyOffice compose app did not report success."
                )
        except DokployApiError as error:
            raise NextcloudError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=updated_compose.compose_id,
        )
        if can_use_stateful_noop_guard:
            persist_compose_artifact_hash(
                state_dir=self._state_dir,
                service_key=self._compose_name,
                rendered_compose=rendered_compose,
            )
        self._created_in_process = True
        self._applied_locator = locator
        return locator

    def _can_use_stateful_noop_guard(self) -> bool:
        return load_state_dir(self._state_dir).applied_state is not None

    def _verify_current_application(self) -> ServiceVerificationResult:
        nextcloud_container = _find_container_name(_nextcloud_service_name(self._stack_name))
        if nextcloud_container is None:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="Nextcloud container is not running; compose app is not ready for a no-op skip.",
            )
        if not _container_health_ready(nextcloud_container):
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="Nextcloud container health check is not ready; compose app is not ready for a no-op skip.",
            )
        nextcloud_url = f"https://{self._nextcloud_hostname}"
        onlyoffice_url = f"https://{self._onlyoffice_hostname}"
        status_url = f"{nextcloud_url}/status.php"
        if not _nextcloud_status_ready(status_url):
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="Nextcloud status.php is not app-ready; compose app is not ready for a no-op skip.",
            )
        if not self.check_health(
            service=NextcloudResourceRecord(
                resource_id="dokploy-compose:verification:onlyoffice-service",
                resource_name=_onlyoffice_service_name(self._stack_name),
            ),
            url=f"{onlyoffice_url}/healthcheck",
        ):
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="downstream",
                status="fail",
                detail="OnlyOffice health check is not ready; compose app is not ready for a no-op skip.",
            )
        try:
            verification = self.ensure_application_ready(
                nextcloud_url=nextcloud_url,
                onlyoffice_url=onlyoffice_url,
            )
        except NextcloudError as error:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="bootstrap",
                status="fail",
                detail=str(error),
            )
        verification_failure = _nextcloud_bundle_verification_failure(verification)
        if verification_failure is not None:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="downstream",
                status="fail",
                detail=verification_failure,
            )
        return ServiceVerificationResult(
            service_name=self._compose_name,
            tier="downstream",
            status="pass",
            detail="Nextcloud, OnlyOffice, and Talk readiness checks passed.",
        )


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


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
) -> RenderedCompose:
    nextcloud_service = _nextcloud_service_name(stack_name)
    onlyoffice_service = _onlyoffice_service_name(stack_name)
    nextcloud_volume = _nextcloud_volume_name(stack_name)
    onlyoffice_volume = _onlyoffice_volume_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    nextcloud_url = f"https://{nextcloud_hostname}"
    onlyoffice_url = f"https://{onlyoffice_hostname}"
    env_specs = [
        _nextcloud_env_spec(
            name="NEXTCLOUD_POSTGRES_HOST",
            value=postgres_service_name,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="shared-core-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_POSTGRES_DB",
            value=postgres.database_name,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="shared-core-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_POSTGRES_USER",
            value=postgres.user_name,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="shared-core-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_POSTGRES_PASSWORD",
            value=_DEFAULT_SHARED_SERVICE_PASSWORD,
            owner="nextcloud-postgres",
            target_services=(nextcloud_service,),
            source=postgres.password_secret_ref,
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_REDIS_HOST",
            value=redis_service_name,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="shared-core-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_REDIS_HOST_PASSWORD",
            value=_nextcloud_shared_redis_password(redis_service_name),
            owner="nextcloud-redis",
            target_services=(nextcloud_service,),
            source=redis.password_secret_ref,
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_ADMIN_USER",
            value=admin_user,
            owner="nextcloud-admin",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="operator-input",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_ADMIN_PASSWORD",
            value=admin_password,
            owner="nextcloud-admin",
            target_services=(nextcloud_service,),
            source="operator-input",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_TRUSTED_DOMAINS",
            value=nextcloud_hostname,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="hostname-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_TRUSTED_PROXIES",
            value=_DEFAULT_TRUSTED_PROXIES,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_OVERWRITEHOST",
            value=nextcloud_hostname,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="hostname-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_OVERWRITEPROTOCOL",
            value="https",
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_OVERWRITECLIURL",
            value=nextcloud_url,
            owner="nextcloud-runtime",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="hostname-plan",
        ),
        _nextcloud_env_spec(
            name="NEXTCLOUD_ONLYOFFICE_URL",
            value=onlyoffice_url,
            owner="nextcloud-onlyoffice-binding",
            target_services=(nextcloud_service,),
            sensitive=False,
            source="hostname-plan",
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_JWT_ENABLED",
            value="true",
            owner="onlyoffice-runtime",
            target_services=(onlyoffice_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_JWT_SECRET",
            value=_nextcloud_generated_secret(integration_secret_ref),
            owner="onlyoffice-jwt",
            target_services=(onlyoffice_service,),
            source=integration_secret_ref,
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_JWT_HEADER",
            value="Authorization",
            owner="onlyoffice-runtime",
            target_services=(onlyoffice_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_ALLOW_PRIVATE_IP_ADDRESS",
            value="true",
            owner="onlyoffice-runtime",
            target_services=(onlyoffice_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_ALLOW_META_IP_ADDRESS",
            value="true",
            owner="onlyoffice-runtime",
            target_services=(onlyoffice_service,),
            sensitive=False,
            source="wizard-default",
        ),
        _nextcloud_env_spec(
            name="ONLYOFFICE_NEXTCLOUD_URL",
            value=nextcloud_url,
            owner="onlyoffice-nextcloud-binding",
            target_services=(onlyoffice_service,),
            sensitive=False,
            source="hostname-plan",
        ),
    ]
    compose_file = (
        "services:\n"
        f"  {nextcloud_service}:\n"
        "    image: nextcloud:apache\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f'      POSTGRES_HOST: "{_required_placeholder("NEXTCLOUD_POSTGRES_HOST")}"\n'
        f'      POSTGRES_DB: "{_required_placeholder("NEXTCLOUD_POSTGRES_DB")}"\n'
        f'      POSTGRES_USER: "{_required_placeholder("NEXTCLOUD_POSTGRES_USER")}"\n'
        f'      POSTGRES_PASSWORD: "{_required_placeholder("NEXTCLOUD_POSTGRES_PASSWORD")}"\n'
        f'      REDIS_HOST: "{_required_placeholder("NEXTCLOUD_REDIS_HOST")}"\n'
        f'      REDIS_HOST_PASSWORD: "{_required_placeholder("NEXTCLOUD_REDIS_HOST_PASSWORD")}"\n'
        f'      NEXTCLOUD_ADMIN_USER: "{_required_placeholder("NEXTCLOUD_ADMIN_USER")}"\n'
        f'      NEXTCLOUD_ADMIN_PASSWORD: "{_required_placeholder("NEXTCLOUD_ADMIN_PASSWORD")}"\n'
        f'      NEXTCLOUD_TRUSTED_DOMAINS: "{_required_placeholder("NEXTCLOUD_TRUSTED_DOMAINS")}"\n'
        f'      TRUSTED_PROXIES: "{_required_placeholder("NEXTCLOUD_TRUSTED_PROXIES")}"\n'
        f'      OVERWRITEHOST: "{_required_placeholder("NEXTCLOUD_OVERWRITEHOST")}"\n'
        f'      OVERWRITEPROTOCOL: "{_required_placeholder("NEXTCLOUD_OVERWRITEPROTOCOL")}"\n'
        f'      OVERWRITECLIURL: "{_required_placeholder("NEXTCLOUD_OVERWRITECLIURL")}"\n'
        f'      ONLYOFFICE_URL: "{_required_placeholder("NEXTCLOUD_ONLYOFFICE_URL")}"\n'
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
        f'      JWT_ENABLED: "{_required_placeholder("ONLYOFFICE_JWT_ENABLED")}"\n'
        f'      JWT_SECRET: "{_required_placeholder("ONLYOFFICE_JWT_SECRET")}"\n'
        f'      JWT_HEADER: "{_required_placeholder("ONLYOFFICE_JWT_HEADER")}"\n'
        f'      ALLOW_PRIVATE_IP_ADDRESS: "{_required_placeholder("ONLYOFFICE_ALLOW_PRIVATE_IP_ADDRESS")}"\n'
        f'      ALLOW_META_IP_ADDRESS: "{_required_placeholder("ONLYOFFICE_ALLOW_META_IP_ADDRESS")}"\n'
        f'      NEXTCLOUD_URL: "{_required_placeholder("ONLYOFFICE_NEXTCLOUD_URL")}"\n'
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
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        "    external: true\n"
    )
    return RenderedCompose(compose_file=compose_file, env_specs=tuple(env_specs))


@dataclass(frozen=True)
class _RenderedComposeApplyResult:
    locator: _ComposeLocator
    status: str


def _apply_rendered_compose_noop_guard(
    *,
    rendered_compose: RenderedCompose,
    service_key: str,
    state_dir: Path,
    client: DokployNextcloudApi,
    locator: _ComposeLocator,
    compose_id: str,
    title: str | None,
    description: str | None,
    verify_current: Callable[[], ServiceVerificationResult],
    locator_factory: Callable[[str], _ComposeLocator],
) -> _RenderedComposeApplyResult:
    rendered_hash = ComposeArtifactHashState.from_rendered_compose(
        service_id=service_key,
        rendered_compose=rendered_compose.compose_file,
        env_specs=rendered_compose.env_specs,
    )
    stored_hash = load_compose_artifact_hash(state_dir=state_dir, service_key=service_key)
    if stored_hash == rendered_hash and verify_current().passed:
        return _RenderedComposeApplyResult(locator=locator, status="already_present")

    updated = _apply_rendered_compose_to_existing(
        client=client,
        compose_id=compose_id,
        rendered_compose=rendered_compose,
    )
    deployment = client.deploy_compose(
        compose_id=updated.compose_id,
        title=title,
        description=description,
    )
    if not deployment.success:
        raise NextcloudError(
            f"Dokploy deploy for compose service '{service_key}' did not report success."
        )
    persist_compose_artifact_hash(
        state_dir=state_dir,
        service_key=service_key,
        rendered_compose=rendered_compose,
    )
    return _RenderedComposeApplyResult(
        locator=locator_factory(updated.compose_id),
        status="applied",
    )


def _apply_rendered_compose_to_existing(
    *,
    client: DokployNextcloudApi,
    compose_id: str,
    rendered_compose: RenderedCompose,
) -> DokployComposeRecord:
    env_payload = DokployEnvReconciler(client=cast(Any, client)).build_env_payload(rendered_compose)
    if env_payload:
        _update_compose_env_if_supported(client, compose_id=compose_id, env_payload=env_payload)
    return client.update_compose(compose_id=compose_id, compose_file=rendered_compose.compose_file)


def _update_compose_env_if_supported(
    client: DokployNextcloudApi, *, compose_id: str, env_payload: str
) -> None:
    update_compose = cast(Any, client).update_compose
    try:
        update_compose(compose_id=compose_id, env=env_payload)
    except TypeError as error:
        message = str(error)
        if "env" not in message and "compose_file" not in message:
            raise


def _nextcloud_env_spec(
    *,
    name: str,
    value: str,
    owner: str,
    target_services: tuple[str, ...],
    source: str,
    sensitive: bool = True,
    required: bool = True,
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(
            name=name,
            value=value,
            sensitive=sensitive,
            source=source,
        ),
        owner=owner,
        target_services=target_services,
        placeholder=_required_placeholder(name) if required else None,
        required=required,
    )


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _nextcloud_generated_secret(secret_ref: str) -> str:
    return "dw-" + sha256(secret_ref.encode("utf-8")).hexdigest()[:32]


def _nextcloud_shared_redis_password(redis_service_name: str) -> str:
    return _nextcloud_generated_secret(f"{redis_service_name}-password")


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


def _container_health_ready(container_name: str) -> bool:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}",
                container_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "healthy"


def _wait_for_nextcloud_first_boot_ready(
    service_name: str,
    status_url: str,
    *,
    attempts: int = _NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_ATTEMPTS,
    delay_seconds: float = _NEXTCLOUD_FIRST_BOOT_CONTAINER_WAIT_DELAY_SECONDS,
) -> str | None:
    status_attempts_remaining = _NEXTCLOUD_FIRST_BOOT_STATUS_WAIT_ATTEMPTS
    for attempt in range(attempts):
        container_name = _find_container_name(service_name)
        if container_name is not None and _container_health_ready(container_name):
            if _nextcloud_status_ready(status_url):
                return container_name
            status_attempts_remaining -= 1
            if status_attempts_remaining <= 0:
                return None
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


def _ensure_onlyoffice_app_config(
    container_name: str,
    document_server_url: str,
    document_server_internal_url: str,
    storage_url: str,
    jwt_secret: str,
    wait_for_documentserver_check: bool = False,
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


def _ensure_ai_integration_openai(
    container_name: str,
    integration: NextcloudAiIntegration,
) -> None:
    _enable_app_with_release_fallback(
        container_name,
        enable_command=f"php occ app:enable --force {_INTEGRATION_OPENAI_APP_ID}",
        install_from_release=_install_integration_openai_app_from_release,
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set {_INTEGRATION_OPENAI_APP_ID} url "
        f"--value={shlex.quote(integration.service_url)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set {_INTEGRATION_OPENAI_APP_ID} api_key "
        f"--value={shlex.quote(integration.api_key)}",
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set {_INTEGRATION_OPENAI_APP_ID} service_name "
        f"--value={shlex.quote(integration.service_name)}",
    )
    _prime_ai_integration_models(container_name, integration)


def _install_integration_openai_app_from_release(container_name: str) -> None:
    _install_nextcloud_app_from_release(
        container_name,
        app_id=_INTEGRATION_OPENAI_APP_ID,
        app_label=_INTEGRATION_OPENAI_APP_LABEL,
    )


def _prime_ai_integration_models(
    container_name: str,
    integration: NextcloudAiIntegration,
) -> None:
    aliases = tuple(
        dict.fromkeys((*integration.expected_models, *_probe_litellm_model_aliases(integration)))
    )
    if not aliases:
        # Nothing to seed yet; the app re-fetches the live list from the gateway
        # whenever an admin opens its settings, so an empty prime is a soft miss.
        return
    models_document = json.dumps(
        {
            "data": [{"id": alias, "object": "model"} for alias in aliases],
            "object": "list",
        },
        separators=(",", ":"),
    )
    _run_occ_shell(
        container_name,
        f"php occ config:app:set {_INTEGRATION_OPENAI_APP_ID} models "
        f"--value={shlex.quote(models_document)}",
    )


def _probe_litellm_model_aliases(integration: NextcloudAiIntegration) -> tuple[str, ...]:
    for attempt in range(_LITELLM_MODELS_PROBE_ATTEMPTS):
        payload: object = None
        try:
            req = request.Request(
                integration.models_probe_url,
                headers={"Authorization": f"Bearer {integration.api_key}"},
                method="GET",
            )
            with request.urlopen(req, timeout=10) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return tuple(
                    dict.fromkeys(
                        model_id
                        for model_id in (
                            entry.get("id")
                            for entry in data
                            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                        )
                        if isinstance(model_id, str) and model_id != ""
                    )
                )
        if attempt < _LITELLM_MODELS_PROBE_ATTEMPTS - 1:
            time.sleep(_LITELLM_MODELS_PROBE_DELAY_SECONDS)
    return ()


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


def _nextcloud_bundle_verification_failure(
    verification: NextcloudBundleVerification,
) -> str | None:
    talk_runtime = verification.talk
    if talk_runtime.enabled is not True:
        return "Nextcloud Talk app 'spreed' is not enabled after bootstrap."
    if talk_runtime.enabled_check.passed is not True:
        return "Nextcloud Talk verification failed while checking app:list output."
    if talk_runtime.signaling_check.passed is not True:
        return "Nextcloud Talk signaling verification failed."
    if talk_runtime.stun_check.passed is not True:
        return "Nextcloud Talk STUN verification failed."
    if talk_runtime.turn_check.passed is not True:
        return "Nextcloud Talk TURN verification failed."
    if verification.onlyoffice_document_server_check.passed is not True:
        return "OnlyOffice document-server verification failed."
    return None


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
