"""Dokploy-backed paired Nextcloud + OnlyOffice runtime backend."""

from __future__ import annotations

import json
import shlex
import ssl
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol
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
from dokploy_wizard.packs.nextcloud.models import NextcloudResourceRecord
from dokploy_wizard.packs.nextcloud.reconciler import NextcloudError

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
        admin_user: str = _DEFAULT_NEXTCLOUD_ADMIN_USER,
        admin_password: str = _DEFAULT_NEXTCLOUD_ADMIN_PASSWORD,
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
        return _local_https_health_check(url)

    def ensure_application_ready(self, *, nextcloud_url: str, onlyoffice_url: str) -> None:
        document_server_url = _with_trailing_slash(onlyoffice_url)
        document_server_internal_url = _with_trailing_slash(
            f"http://{_onlyoffice_service_name(self._stack_name)}"
        )
        storage_url = _with_trailing_slash(f"http://{_nextcloud_service_name(self._stack_name)}")
        if _nextcloud_status_ready(f"{nextcloud_url}/status.php"):
            container = _find_container_name(_nextcloud_service_name(self._stack_name))
            if container is None:
                return
            _ensure_admin_user(container, self._admin_user, self._admin_password)
            _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
            _ensure_onlyoffice_app_config(
                container,
                document_server_url=document_server_url,
                document_server_internal_url=document_server_internal_url,
                storage_url=storage_url,
                jwt_secret=_DEFAULT_SHARED_SERVICE_PASSWORD,
            )
            _run_occ_shell(container, "php occ app:install spreed || true")
            _run_occ_shell(container, "php occ app:enable spreed")
            return
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
        _ensure_trusted_domain(container, _nextcloud_service_name(self._stack_name))
        _ensure_onlyoffice_app_config(
            container,
            document_server_url=document_server_url,
            document_server_internal_url=document_server_internal_url,
            storage_url=storage_url,
            jwt_secret=_DEFAULT_SHARED_SERVICE_PASSWORD,
        )
        _run_occ_shell(container, "php occ app:install spreed || true")
        _run_occ_shell(container, "php occ app:enable spreed")

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
) -> str:
    nextcloud_service = _nextcloud_service_name(stack_name)
    onlyoffice_service = _onlyoffice_service_name(stack_name)
    nextcloud_volume = _nextcloud_volume_name(stack_name)
    onlyoffice_volume = _onlyoffice_volume_name(stack_name)
    shared_network = _shared_network_name(stack_name)
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
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
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
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
        "volumes:\n"
        f"  {nextcloud_volume}:\n"
        f"  {onlyoffice_volume}:\n"
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
            ["docker", "ps", "--format", "{{.Names}}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if service_name in line:
            return line
    return None


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
    command = [
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
    _run_occ_command(command, args)


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
) -> None:
    def_formats = json.dumps(_DEFAULT_ONLYOFFICE_DEF_FORMATS, separators=(",", ":"), sort_keys=True)
    edit_formats = json.dumps(
        _DEFAULT_ONLYOFFICE_EDIT_FORMATS,
        separators=(",", ":"),
        sort_keys=True,
    )
    _run_occ_shell(container_name, "php occ app:install onlyoffice || true")
    _run_occ_shell(container_name, "php occ app:enable --force onlyoffice")
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
    _run_occ_shell(container_name, "php occ onlyoffice:documentserver --check")


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
