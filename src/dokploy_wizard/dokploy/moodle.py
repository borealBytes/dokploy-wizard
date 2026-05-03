# ruff: noqa: E501
"""Dokploy-backed Moodle runtime backend."""

from __future__ import annotations

import re
import shlex
import ssl
import subprocess
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
    DokployScheduleRecord,
)
from dokploy_wizard.packs.moodle import MoodleError, MoodleResourceRecord

_DEFAULT_SHARED_SERVICE_PASSWORD = "change-me"
_DEFAULT_MOODLE_FULLNAME = "Moodle"
_DEFAULT_MOODLE_SHORTNAME = "Moodle"
_DEFAULT_MOODLE_CRON = "* * * * *"
_DEFAULT_MOODLE_CRON_TIMEZONE = "UTC"
_DEFAULT_MOODLE_CONFIG_CACHE = "/var/moodledata/config.php"
_DEFAULT_MOODLE_DATAROOT = "/var/moodledata/files"
_DEFAULT_MOODLE_DOCROOT = "/var/www/html"
_DEFAULT_MOODLE_SOURCE_REF = "MOODLE_500_STABLE"
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_DEFAULT_MOODLE_PLUGIN_PARENT_PATHS = (
    "/var/www/html/admin/tool",
    "/var/www/html/assign/feedback",
    "/var/www/html/assign/submission",
    "/var/www/html/auth",
    "/var/www/html/availability/condition",
    "/var/www/html/badges",
    "/var/www/html/blocks",
    "/var/www/html/course/format",
    "/var/www/html/dataformat",
    "/var/www/html/editor",
    "/var/www/html/enrol",
    "/var/www/html/filter",
    "/var/www/html/grade/export",
    "/var/www/html/grade/import",
    "/var/www/html/grade/report",
    "/var/www/html/local",
    "/var/www/html/mod",
    "/var/www/html/payment/gateway",
    "/var/www/html/plagiarism",
    "/var/www/html/portfolio",
    "/var/www/html/profile/field",
    "/var/www/html/question/behaviour",
    "/var/www/html/question/format",
    "/var/www/html/question/type",
    "/var/www/html/report",
    "/var/www/html/repository",
    "/var/www/html/search/engine",
    "/var/www/html/theme",
    "/var/www/html/webservice",
)


class DokployMoodleApi(Protocol):
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


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokployMoodleBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        admin_email: str,
        admin_password: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_from_address: str | None = None,
        moodle_cron: str = _DEFAULT_MOODLE_CRON,
        moodle_cron_timezone: str = _DEFAULT_MOODLE_CRON_TIMEZONE,
        client: DokployMoodleApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_from_address = smtp_from_address
        self._moodle_cron = moodle_cron
        self._moodle_cron_timezone = moodle_cron_timezone
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> MoodleResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return MoodleResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name),
        )

    def find_service_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return MoodleResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(self, **kwargs: object) -> MoodleResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise MoodleError("Moodle service name does not match the active Dokploy plan.")
        if hostname != self._hostname:
            raise MoodleError("Moodle hostname no longer matches the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise MoodleError("Moodle postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise MoodleError("Moodle data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return MoodleResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def update_service(self, **kwargs: object) -> MoodleResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> MoodleResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return MoodleResourceRecord(
            resource_id=resource_id,
            resource_name=_data_name(self._stack_name),
        )

    def find_persistent_data_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return MoodleResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> MoodleResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise MoodleError("Moodle data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return MoodleResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: MoodleResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(self) -> tuple[str, ...]:
        container_name = (
            _wait_for_container_name(_service_name(self._stack_name))
            if self._created_in_process
            else _find_container_name(_service_name(self._stack_name))
        )
        if container_name is None:
            raise MoodleError("Moodle container is not running; cannot finish application bootstrap.")
        _prepare_moodle_runtime(container_name)
        notes: list[str] = []
        if _moodle_is_initialized(container_name):
            notes.append("Moodle already initialized; skipped CLI install.")
        else:
            _install_moodle(
                container_name=container_name,
                hostname=self._hostname,
                postgres_service_name=self._postgres_service_name,
                postgres=self._postgres,
                admin_email=self._admin_email,
                admin_password=self._admin_password,
            )
            _repair_moodle_config_file(container_name, f"{_DEFAULT_MOODLE_DOCROOT}/config.php")
            _persist_moodle_config(container_name)
            _wait_for_local_https_health(f"https://{self._hostname}/login/index.php")
            notes.append("Installed Moodle via admin/cli/install.php.")
        if self._smtp_host is not None and self._smtp_port is not None and self._smtp_from_address is not None:
            _configure_moodle_smtp(
                container_name,
                smtp_host=self._smtp_host,
                smtp_port=self._smtp_port,
                from_address=self._smtp_from_address,
            )
            notes.append(f"Configured Moodle outbound mail via '{self._smtp_host}:{self._smtp_port}'.")
        try:
            self._ensure_moodle_cron_schedule()
        except DokployApiError as error:
            notes.append(
                "Skipped Moodle cron schedule reconciliation because Dokploy schedule auth is "
                f"not available yet: {error}"
            )
        else:
            notes.append(
                f"Ensured managed Moodle cron schedule '{_cron_schedule_name(self._stack_name)}'."
            )
        return tuple(notes)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise MoodleError(str(error)) from error
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
            postgres_service_name=self._postgres_service_name,
            postgres=self._postgres,
        )
        try:
            if self._applied_locator is not None:
                updated = self._client.update_compose(
                    compose_id=self._applied_locator.compose_id,
                    compose_file=compose_file,
                )
                self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard moodle reconcile",
                    description="Update Moodle compose app",
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
                            title="dokploy-wizard moodle reconcile",
                            description="Update Moodle compose app",
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
                    title="dokploy-wizard moodle reconcile",
                    description="Create Moodle compose app",
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
                title="dokploy-wizard moodle reconcile",
                description="Create Moodle compose app",
            )
        except DokployApiError as error:
            raise MoodleError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._created_in_process = True
        self._applied_locator = locator
        return locator

    def _ensure_moodle_cron_schedule(self) -> None:
        locator = self._find_compose_locator()
        if locator is None:
            raise MoodleError("Moodle compose locator is unavailable for cron reconciliation.")
        schedule_name = _cron_schedule_name(self._stack_name)
        service_name = _service_name(self._stack_name)
        command = f"php {_DEFAULT_MOODLE_DOCROOT}/admin/cli/cron.php >/dev/null 2>&1"
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
                cron_expression=self._moodle_cron,
                timezone=self._moodle_cron_timezone,
                shell_type="bash",
                command=command,
                enabled=True,
            )
            return
        if (
            existing.service_name != service_name
            or existing.cron_expression != self._moodle_cron
            or existing.timezone != self._moodle_cron_timezone
            or existing.shell_type != "bash"
            or existing.command != command
            or existing.enabled is not True
        ):
            self._client.update_schedule(
                schedule_id=existing.schedule_id,
                name=schedule_name,
                compose_id=locator.compose_id,
                service_name=service_name,
                cron_expression=self._moodle_cron,
                timezone=self._moodle_cron_timezone,
                shell_type="bash",
                command=command,
                enabled=True,
            )


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
    return f"{stack_name}-moodle"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-moodle-data"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _cron_schedule_name(stack_name: str) -> str:
    return f"{stack_name}-moodle-cron"


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
) -> str:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    runtime_prepare_shell = _compose_escape_shell(_moodle_runtime_prepare_shell())
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: moodlehq/moodle-php-apache:8.4-bullseye\n"
        "    restart: unless-stopped\n"
        "    command:\n"
        "      - /bin/sh\n"
        "      - -lc\n"
        "      - |\n"
        + "\n".join(f"        {line}" for line in runtime_prepare_shell.splitlines())
        + "\n"
        "        exec apache2-foreground\n"
        "    environment:\n"
        f"      DOKPLOY_WIZARD_MOODLE_WWWROOT: {_yaml_quote(f'https://{hostname}')}\n"
        f"      DOKPLOY_WIZARD_MOODLE_DATAROOT: {_yaml_quote(_DEFAULT_MOODLE_DATAROOT)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_DBHOST: {_yaml_quote(postgres_service_name)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_DBNAME: {_yaml_quote(postgres.database_name)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_DBUSER: {_yaml_quote(postgres.user_name)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_DBPASS: {_yaml_quote(_DEFAULT_SHARED_SERVICE_PASSWORD)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_CONFIG_CACHE: {_yaml_quote(_DEFAULT_MOODLE_CONFIG_CACHE)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_SOURCE_REF: {_yaml_quote(_DEFAULT_MOODLE_SOURCE_REF)}\n"
        f"      DOKPLOY_WIZARD_MOODLE_SOURCE_ARCHIVE_URL: {_yaml_quote(_moodle_source_archive_url())}\n"
        f"      DOKPLOY_WIZARD_MOODLE_TRUSTED_PROXIES: {_yaml_quote(_DEFAULT_TRUSTED_PROXIES)}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "80"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'curl -fsS http://127.0.0.1/login/index.php >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:/var/moodledata\n"
        "    expose:\n"
        "      - '80'\n"
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
    except urlerror.HTTPError as exc:
        return exc.code < 500
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


def _wait_for_local_https_health(
    url: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _local_https_health_check(url):
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
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def _wait_for_container_name(
    service_name: str, *, attempts: int = 24, delay_seconds: float = 5.0
) -> str | None:
    for attempt in range(attempts):
        container_name = _find_container_name(service_name)
        if container_name is not None:
            return container_name
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _prepare_moodle_runtime(container_name: str) -> None:
    _run_container_shell(
        container_name,
        _moodle_runtime_prepare_shell(),
        error_prefix="Unable to prepare Moodle runtime directories",
    )


def _moodle_is_initialized(container_name: str) -> bool:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"test -f {_DEFAULT_MOODLE_CONFIG_CACHE} || test -f {_DEFAULT_MOODLE_DOCROOT}/config.php",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    probe = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"php {_DEFAULT_MOODLE_DOCROOT}/admin/cli/isinstalled.php >/dev/null 2>&1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _install_moodle(
    *,
    container_name: str,
    hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    admin_email: str,
    admin_password: str,
) -> None:
    admin_username = _moodle_admin_username_from_email(admin_email)
    install_command = " ".join(
        [
            "php",
            shlex.quote(f"{_DEFAULT_MOODLE_DOCROOT}/admin/cli/install.php"),
            "--non-interactive",
            "--agree-license",
            f"--wwwroot={shlex.quote(f'https://{hostname}')}",
            f"--dataroot={shlex.quote(_DEFAULT_MOODLE_DATAROOT)}",
            "--dbtype=pgsql",
            f"--dbhost={shlex.quote(postgres_service_name)}",
            "--dbport=5432",
            f"--dbname={shlex.quote(postgres.database_name)}",
            f"--dbuser={shlex.quote(postgres.user_name)}",
            f"--dbpass={shlex.quote(_DEFAULT_SHARED_SERVICE_PASSWORD)}",
            f"--fullname={shlex.quote(_DEFAULT_MOODLE_FULLNAME)}",
            f"--shortname={shlex.quote(_DEFAULT_MOODLE_SHORTNAME)}",
            f"--adminuser={shlex.quote(admin_username)}",
            f"--adminpass={shlex.quote(admin_password)}",
            f"--adminemail={shlex.quote(admin_email)}",
        ]
    )
    _run_container_shell(
        container_name,
        f"set -eu && {install_command}",
        error_prefix="Moodle CLI install failed",
    )


def _persist_moodle_config(container_name: str) -> None:
    _run_container_shell(
        container_name,
        (
            "set -eu && "
            f"cp {_DEFAULT_MOODLE_DOCROOT}/config.php {_DEFAULT_MOODLE_CONFIG_CACHE} && chmod 0644 {_DEFAULT_MOODLE_CONFIG_CACHE} && chmod 0644 {_DEFAULT_MOODLE_DOCROOT}/config.php"
        ),
        error_prefix="Unable to persist Moodle config.php into managed data storage",
    )


def _configure_moodle_smtp(
    container_name: str,
    *,
    smtp_host: str,
    smtp_port: int,
    from_address: str,
) -> None:
    cfg = f"{_DEFAULT_MOODLE_DOCROOT}/admin/cli/cfg.php"
    commands = [
        f"php {cfg} --name=smtphosts --set={shlex.quote(f'{smtp_host}:{smtp_port}')}",
        f"php {cfg} --name=smtpsecure --set=''",
        f"php {cfg} --name=smtpauthtype --set=''",
        f"php {cfg} --name=smtpuser --set=''",
        f"php {cfg} --name=smtppass --set=''",
        f"php {cfg} --name=noreplyaddress --set={shlex.quote(from_address)}",
    ]
    _run_container_shell(
        container_name,
        "set -eu && " + " && ".join(commands),
        error_prefix="Unable to configure Moodle SMTP",
    )


def _repair_moodle_config_file(container_name: str, config_path: str) -> None:
    _run_container_shell(
        container_name,
        _moodle_proxy_config_patch_shell(config_path),
        error_prefix="Unable to patch Moodle proxy config",
    )


def _run_container_shell(container_name: str, shell_command: str, *, error_prefix: str) -> None:
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", shell_command],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MoodleError(f"{error_prefix}: {detail or 'unknown error'}")


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _moodle_source_archive_url() -> str:
    return f"https://github.com/moodle/moodle/archive/refs/heads/{_DEFAULT_MOODLE_SOURCE_REF}.tar.gz"


def _compose_escape_shell(shell_script: str) -> str:
    return shell_script.replace("$", "$$")


def _moodle_proxy_config_patch_shell(config_path: str) -> str:
    php_patch = " ".join(
        [
            "php",
            "-r",
            shlex.quote(
                """
$path = $argv[1];
$content = @file_get_contents($path);
if ($content === false) {
    fwrite(STDERR, \"Unable to read Moodle config\\n\");
    exit(1);
}
if (strpos($content, '$CFG->sslproxy = true;') === false) {
    $proxy = '$CFG->sslproxy = true;' . "\\n";
    $pattern = \"/require_once\\(__DIR__ \\\\. ['\\\"]\\/lib\\/setup\\.php['\\\"]\\);/\";
    $replacement = $proxy . \"require_once(__DIR__ . '/lib/setup.php');\";
    $count = 0;
    $updated = preg_replace($pattern, $replacement, $content, 1, $count);
    if (!is_string($updated) || $count !== 1) {
        fwrite(STDERR, \"Unable to patch Moodle proxy config\\n\");
        exit(1);
    }
    if (file_put_contents($path, $updated) === false) {
        fwrite(STDERR, \"Unable to write Moodle config\\n\");
        exit(1);
    }
}
""".strip()
            ),
            shlex.quote(config_path),
        ]
    )
    return f"set -eu && if [ -f {config_path} ]; then {php_patch} && chmod 0644 {config_path}; fi"


def _moodle_runtime_prepare_shell() -> str:
    source_archive_url = shlex.quote(_moodle_source_archive_url())
    plugin_parent_paths = " ".join(shlex.quote(path) for path in _DEFAULT_MOODLE_PLUGIN_PARENT_PATHS)
    return (
        "set -eu\n"
        f"mkdir -p {_DEFAULT_MOODLE_DATAROOT}\n"
        f"if [ ! -f {_DEFAULT_MOODLE_DOCROOT}/admin/cli/install.php ]; then\n"
        "  tmp_archive=/tmp/moodle-source.tar.gz\n"
        f"  curl -fsSL {source_archive_url} -o \"${{tmp_archive}}\"\n"
        f"  mkdir -p {_DEFAULT_MOODLE_DOCROOT}\n"
        f"  tar -xzf \"${{tmp_archive}}\" -C {_DEFAULT_MOODLE_DOCROOT} --strip-components=1\n"
        "  rm -rf \"${tmp_archive}\"\n"
        "fi\n"
        f"if [ -f {_DEFAULT_MOODLE_CONFIG_CACHE} ]; then chmod 0644 {_DEFAULT_MOODLE_CONFIG_CACHE} && rm -f {_DEFAULT_MOODLE_DOCROOT}/config.php && cp {_DEFAULT_MOODLE_CONFIG_CACHE} {_DEFAULT_MOODLE_DOCROOT}/config.php && chmod 0644 {_DEFAULT_MOODLE_DOCROOT}/config.php; fi\n"
        f"chown -R www-data:www-data {_DEFAULT_MOODLE_DATAROOT}\n"
        f"find {_DEFAULT_MOODLE_DATAROOT} -type d -exec chmod 0770 {{}} +\n"
        f"find {_DEFAULT_MOODLE_DATAROOT} -type f -exec chmod 0660 {{}} +\n"
        f"for plugin_parent in {plugin_parent_paths}; do\n"
        "  if [ -d \"${plugin_parent}\" ]; then\n"
        "    chown root:www-data \"${plugin_parent}\"\n"
        "    chmod 2775 \"${plugin_parent}\"\n"
        "  fi\n"
        "done\n"
        f"{_moodle_proxy_config_patch_shell(f'{_DEFAULT_MOODLE_DOCROOT}/config.php')}"
    )


def _moodle_admin_username_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", local_part).strip("_")
    collapsed = re.sub(r"_+", "_", normalized)
    if collapsed == "":
        return "admin"
    return collapsed[:100]
