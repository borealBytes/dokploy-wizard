# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed DocuSeal runtime backend."""

from __future__ import annotations

import re
import ssl
import time
from dataclasses import dataclass
from hashlib import sha256
from http import cookiejar
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
from dokploy_wizard.packs.docuseal import (
    DocuSealBootstrapState,
    DocuSealError,
    DocuSealResourceRecord,
)

_DEFAULT_SHARED_SERVICE_PASSWORD = "change-me"
_DEFAULT_DOCUSEAL_IMAGE = "docuseal/docuseal:latest"
_DEFAULT_DOCUSEAL_PORT = "3000"
_DEFAULT_DOCUSEAL_DATA_ROOT = "/data/docuseal"
_DEFAULT_DOCUSEAL_ACCOUNT_NAME = "Dokploy Wizard"
_DOCUSEAL_HEALTHCHECK_COMMAND = (
    'ruby -rnet/http -e "uri = URI(%q{http://127.0.0.1:3000/up}); '
    'response = Net::HTTP.get_response(uri); '
    'exit(response.is_a?(Net::HTTPSuccess) ? 0 : 1)"'
)


class DokployDocuSealApi(Protocol):
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


@dataclass(frozen=True)
class _DocuSealSetupProbe:
    status: int
    location: str | None
    body: str


@dataclass(frozen=True)
class _DocuSealSetupFormContext:
    authenticity_token: str | None
    timezone: str
    locale: str


class DokployDocuSealBackend:
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
        smtp_domain: str | None = None,
        smtp_from_address: str | None = None,
        client: DokployDocuSealApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._secret_key_base_secret_ref = _secret_key_base_secret_ref(stack_name)
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_domain = smtp_domain
        self._smtp_from_address = smtp_from_address
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> DocuSealResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return DocuSealResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name),
        )

    def find_service_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return DocuSealResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(self, **kwargs: object) -> DocuSealResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise DocuSealError("DocuSeal service name does not match the active Dokploy plan.")
        if hostname != self._hostname:
            raise DocuSealError("DocuSeal hostname no longer matches the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise DocuSealError("DocuSeal postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise DocuSealError("DocuSeal data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return DocuSealResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def update_service(self, **kwargs: object) -> DocuSealResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> DocuSealResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return DocuSealResourceRecord(
            resource_id=resource_id,
            resource_name=_data_name(self._stack_name),
        )

    def find_persistent_data_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return DocuSealResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> DocuSealResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise DocuSealError("DocuSeal data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return DocuSealResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: DocuSealResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(
        self, *, secret_key_base_secret_ref: str
    ) -> tuple[DocuSealBootstrapState, tuple[str, ...]]:
        if secret_key_base_secret_ref != self._secret_key_base_secret_ref:
            raise DocuSealError(
                "DocuSeal SECRET_KEY_BASE secret ref no longer matches the active Dokploy plan."
            )
        health_url = _health_url(self._hostname)
        if self._created_in_process and not _wait_for_local_https_health(health_url):
            raise DocuSealError("DocuSeal /up endpoint did not become locally reachable before setup.")
        notes: list[str] = []
        if _docuseal_is_initialized(self._hostname):
            notes.append("DocuSeal already initialized; skipped internal setup flow.")
            return (
                DocuSealBootstrapState(
                    initialized=True,
                    secret_key_base_secret_ref=secret_key_base_secret_ref,
                ),
                tuple(notes),
            )
        _submit_docuseal_setup(
            hostname=self._hostname,
            admin_email=self._admin_email,
            admin_password=self._admin_password,
        )
        if not _wait_for_docuseal_initialization(self._hostname):
            raise DocuSealError("DocuSeal setup completed but initialization state did not converge.")
        notes.append("Initialized DocuSeal via internal /setup flow.")
        return (
            DocuSealBootstrapState(
                initialized=True,
                secret_key_base_secret_ref=secret_key_base_secret_ref,
            ),
            tuple(notes),
        )

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise DocuSealError(str(error)) from error
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
            secret_key_base_secret_ref=self._secret_key_base_secret_ref,
            smtp_host=self._smtp_host,
            smtp_port=self._smtp_port,
            smtp_domain=self._smtp_domain,
            smtp_from_address=self._smtp_from_address,
        )
        try:
            if self._applied_locator is not None:
                updated = self._client.update_compose(
                    compose_id=self._applied_locator.compose_id,
                    compose_file=compose_file,
                )
                self._client.deploy_compose(
                    compose_id=updated.compose_id,
                    title="dokploy-wizard docuseal reconcile",
                    description="Update DocuSeal compose app",
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
                            title="dokploy-wizard docuseal reconcile",
                            description="Update DocuSeal compose app",
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
                    title="dokploy-wizard docuseal reconcile",
                    description="Create DocuSeal compose app",
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
                title="dokploy-wizard docuseal reconcile",
                description="Create DocuSeal compose app",
            )
        except DokployApiError as error:
            raise DocuSealError(str(error)) from error
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
    return f"{stack_name}-docuseal"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-docuseal-data"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _secret_key_base_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-docuseal-secret-key-base"


def _postgres_password(postgres: SharedPostgresAllocation) -> str:
    del postgres
    return _DEFAULT_SHARED_SERVICE_PASSWORD


def _database_url(postgres_service_name: str, postgres: SharedPostgresAllocation) -> str:
    return (
        f"postgres://{postgres.user_name}:{_postgres_password(postgres)}"
        f"@{postgres_service_name}:5432/{postgres.database_name}?sslmode=disable"
    )


def _secret_key_base_value(stack_name: str, secret_key_base_secret_ref: str) -> str:
    material = f"dokploy-wizard:docuseal:{stack_name}:{secret_key_base_secret_ref}"
    return sha256(material.encode("utf-8")).hexdigest()


def _health_url(hostname: str) -> str:
    return f"https://{hostname}/up"


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    secret_key_base_secret_ref: str,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_domain: str | None = None,
    smtp_from_address: str | None = None,
) -> str:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    health_url = _health_url(hostname)
    secret_key_base = _secret_key_base_value(stack_name, secret_key_base_secret_ref)
    smtp_block = ""
    if smtp_host is not None and smtp_port is not None and smtp_from_address is not None:
        smtp_block = (
            f"      SMTP_ADDRESS: {smtp_host}\n"
            f"      SMTP_PORT: '{smtp_port}'\n"
            f"      SMTP_DOMAIN: {smtp_domain or hostname}\n"
            f"      SMTP_FROM: {_yaml_quote(smtp_from_address)}\n"
            "      SMTP_ENABLE_STARTTLS: 'false'\n"
        )
    return (
        "services:\n"
        f"  {service_name}:\n"
        f"    image: {_DEFAULT_DOCUSEAL_IMAGE}\n"
        "    restart: unless-stopped\n"
        f"    working_dir: {_DEFAULT_DOCUSEAL_DATA_ROOT}\n"
        "    environment:\n"
        f"      DATABASE_URL: {_yaml_quote(_database_url(postgres_service_name, postgres))}\n"
        f"      SECRET_KEY_BASE: {secret_key_base}\n"
        f"      DOKPLOY_WIZARD_DOCUSEAL_BASE_URL: {_yaml_quote(f'https://{hostname}')}\n"
        f"      DOKPLOY_WIZARD_DOCUSEAL_DATA_ROOT: {_yaml_quote(_DEFAULT_DOCUSEAL_DATA_ROOT)}\n"
        f"{smtp_block}"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "{_DEFAULT_DOCUSEAL_PORT}"\n'
        "    healthcheck:\n"
        f"      test: ['CMD-SHELL', '{_DOCUSEAL_HEALTHCHECK_COMMAND}']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:{_DEFAULT_DOCUSEAL_DATA_ROOT}\n"
        "    expose:\n"
        f"      - '{_DEFAULT_DOCUSEAL_PORT}'\n"
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
        f"# Managed health endpoint: {health_url}\n"
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


def _wait_for_local_https_health(
    url: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _local_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _wait_for_docuseal_initialization(
    hostname: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _docuseal_is_initialized(hostname):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _docuseal_is_initialized(hostname: str) -> bool:
    probe = _probe_docuseal_setup(hostname)
    return _docuseal_setup_probe_indicates_initialized(probe)


def _probe_docuseal_setup(hostname: str) -> _DocuSealSetupProbe:
    req = _loopback_request(hostname=hostname, path="/setup", method="GET")
    try:
        opener = urlrequest.build_opener(_NoRedirectHandler(), urlrequest.HTTPSHandler(context=_https_context()))
        with opener.open(req, timeout=20) as response:
            return _DocuSealSetupProbe(
                status=response.status,
                location=response.headers.get("Location"),
                body=response.read().decode("utf-8", "ignore"),
            )
    except urlerror.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers is not None else None
        body = exc.read().decode("utf-8", "ignore")
        return _DocuSealSetupProbe(status=exc.code, location=location, body=body)


def _docuseal_setup_probe_indicates_initialized(probe: _DocuSealSetupProbe) -> bool:
    if probe.status == 404:
        return False
    if probe.status in {301, 302, 303, 307, 308}:
        location = (probe.location or "").lower()
        return "/setup" not in location
    if probe.status in {401, 403}:
        return True
    if probe.status != 200:
        return False
    body = probe.body.lower()
    if any(
        marker in body
        for marker in (
            "initial setup",
            'action="/setup"',
            "setup docuseal",
            "create your account",
            'name="user[email]"',
            'name="user[password]"',
            'name="account[name]"',
            'name="app_url"',
            "company name",
        )
    ):
        return False
    return True


def _submit_docuseal_setup(*, hostname: str, admin_email: str, admin_password: str) -> None:
    opener = urlrequest.build_opener(
        _NoRedirectHandler(),
        urlrequest.HTTPSHandler(context=_https_context()),
        urlrequest.HTTPCookieProcessor(cookiejar.CookieJar()),
    )
    context = _fetch_docuseal_setup_form_context(hostname, opener=opener)
    payload = parse.urlencode(
        _docuseal_setup_form(
            hostname,
            admin_email,
            admin_password,
            authenticity_token=context.authenticity_token,
            timezone=context.timezone,
            locale=context.locale,
        )
    ).encode("utf-8")
    req = _loopback_request(
        hostname=hostname,
        path="/setup",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read().decode("utf-8", "ignore")
            probe = _DocuSealSetupProbe(
                status=response.status,
                location=response.headers.get("Location"),
                body=body,
            )
            if response.status not in {200, 201, 302, 303}:
                raise DocuSealError(
                    f"DocuSeal internal /setup flow returned unexpected HTTP {response.status}."
                )
            if not _docuseal_setup_probe_indicates_initialized(probe):
                raise DocuSealError(
                    "DocuSeal internal /setup flow did not converge; /setup still renders the initial setup form."
                )
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        probe = _DocuSealSetupProbe(
            status=exc.code,
            location=exc.headers.get("Location") if exc.headers is not None else None,
            body=body,
        )
        if exc.code not in {200, 201, 302, 303}:
            raise DocuSealError(
                f"DocuSeal internal /setup flow failed with HTTP {exc.code}."
            ) from exc
        if not _docuseal_setup_probe_indicates_initialized(probe):
            raise DocuSealError(
                "DocuSeal internal /setup flow did not converge; /setup still renders the initial setup form."
            )


def _fetch_docuseal_setup_form_context(
    hostname: str, *, opener: object | None = None
) -> _DocuSealSetupFormContext:
    active_opener = opener or urlrequest.build_opener(
        _NoRedirectHandler(),
        urlrequest.HTTPSHandler(context=_https_context()),
        urlrequest.HTTPCookieProcessor(cookiejar.CookieJar()),
    )
    req = _loopback_request(hostname=hostname, path="/setup", method="GET")
    try:
        with active_opener.open(req, timeout=20) as response:  # type: ignore[attr-defined]
            body = response.read().decode("utf-8", "ignore")
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        if exc.code != 200:
            raise DocuSealError(
                f"DocuSeal setup form probe failed with HTTP {exc.code}."
            ) from exc
    return _docuseal_setup_form_context(body)


def _docuseal_setup_form_context(body: str) -> _DocuSealSetupFormContext:
    return _DocuSealSetupFormContext(
        authenticity_token=_html_meta_content(body, "csrf-token") or _html_input_value(
            body, "authenticity_token"
        ),
        timezone=_html_input_value(body, "account[timezone]") or "UTC",
        locale=_html_select_selected_value(body, "account[locale]")
        or _html_input_value(body, "account[locale]")
        or "en-US",
    )


def _docuseal_setup_form(
    hostname: str,
    admin_email: str,
    admin_password: str,
    *,
    authenticity_token: str | None,
    timezone: str,
    locale: str,
) -> dict[str, str]:
    first_name, last_name = _docuseal_admin_name(admin_email)
    form = {
        "user[email]": admin_email,
        "user[first_name]": first_name,
        "user[last_name]": last_name,
        "user[password]": admin_password,
        "user[password_confirmation]": admin_password,
        "account[timezone]": timezone,
        "account[name]": _DEFAULT_DOCUSEAL_ACCOUNT_NAME,
        "account[locale]": locale,
        "encrypted_config[value]": f"https://{hostname}",
    }
    if authenticity_token:
        form["authenticity_token"] = authenticity_token
    return form


def _docuseal_admin_name(email: str) -> tuple[str, str]:
    local_part = email.split("@", 1)[0].strip()
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", local_part) if token]
    if not tokens:
        return ("Dokploy", "Admin")
    if len(tokens) == 1:
        return (tokens[0].title(), "Admin")
    return (tokens[0].title(), " ".join(token.title() for token in tokens[1:]))


def _loopback_request(
    *,
    hostname: str,
    path: str,
    method: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> urlrequest.Request:
    request_headers = {"Accept": "text/html,application/json", "Host": hostname}
    if headers is not None:
        request_headers.update(headers)
    return urlrequest.Request(
        f"https://127.0.0.1{path}",
        data=data,
        headers=request_headers,
        method=method,
    )


def _html_meta_content(body: str, name: str) -> str | None:
    pattern = rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']'
    match = re.search(pattern, body, re.IGNORECASE)
    return match.group(1) if match else None


def _html_input_value(body: str, name: str) -> str | None:
    pattern = (
        rf'<input[^>]+name=["\']{re.escape(name)}["\'][^>]+value=["\']([^"\']*)["\']'
        rf'|<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']'
    )
    match = re.search(pattern, body, re.IGNORECASE)
    if not match:
        return None
    return next((group for group in match.groups() if group is not None), None)


def _html_select_selected_value(body: str, name: str) -> str | None:
    select_pattern = rf'<select[^>]+name=["\']{re.escape(name)}["\'][^>]*>(.*?)</select>'
    select_match = re.search(select_pattern, body, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return None
    option_match = re.search(
        r'<option[^>]+value=["\']([^"\']+)["\'][^>]*selected[^>]*>',
        select_match.group(1),
        re.IGNORECASE,
    )
    return option_match.group(1) if option_match else None


def _https_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        del req, fp, code, msg, headers, newurl
        return None
