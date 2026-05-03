"""Dokploy-backed shared-core backend using a compose-first deployment flow."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.core import (
    SharedCoreError,
    SharedCorePlan,
    SharedCoreResourceRecord,
    SharedPostgresAllocation,
)
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.litellm import build_litellm_config, render_litellm_config_yaml


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
        mail_relay_config: dict[str, str] | None = None,
        litellm_env: dict[str, str] | None = None,
        client: DokploySharedCoreApi | None = None,
        allocation_provisioner: Callable[[tuple[SharedPostgresAllocation, ...]], None]
        | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._plan = plan
        self._compose_name = plan.network_name
        self._mail_relay_config = mail_relay_config or {}
        self._litellm_env = litellm_env or {}
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._allocation_provisioner = allocation_provisioner

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

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        if self._plan.postgres is None or not allocations:
            return
        if self._allocation_provisioner is not None:
            self._allocation_provisioner(allocations)
            return
        container_name = _wait_for_container_name(self._plan.postgres.service_name)
        if container_name is None:
            raise SharedCoreError(
                "Shared-core Postgres container is not running; "
                "cannot provision per-pack databases."
            )
        _wait_for_postgres_ready(container_name)
        for allocation in allocations:
            password = _postgres_password_for_allocation(allocation)
            _ensure_postgres_role(container_name, allocation.user_name, password)
            _ensure_postgres_database(
                container_name,
                allocation.database_name,
                allocation.user_name,
            )

    def validate_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> bool:
        if self._plan.postgres is None or not allocations:
            return True
        container_name = _find_container_name(self._plan.postgres.service_name)
        if container_name is None:
            return False
        for allocation in allocations:
            if not _can_connect_as_allocation(container_name, allocation):
                return False
        return True

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

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self._plan.mail_relay is None or self._lookup_locator(resource_id, "postfix") is None:
            return None
        if _find_container_name(self._plan.mail_relay.service_name) is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=resource_id,
            resource_name=self._plan.mail_relay.service_name,
        )

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self._plan.mail_relay is None or resource_name != self._plan.mail_relay.service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        if _find_container_name(resource_name) is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "postfix"),
            resource_name=resource_name,
        )

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        if self._plan.mail_relay is None or resource_name != self._plan.mail_relay.service_name:
            raise SharedCoreError("Shared-core mail relay name does not match the active plan.")
        locator = self._ensure_compose_applied()
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "postfix"),
            resource_name=resource_name,
        )

    def get_litellm_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self._plan.litellm is None or self._lookup_locator(resource_id, "litellm") is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=resource_id,
            resource_name=self._plan.litellm.service_name,
        )

    def find_litellm_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self._plan.litellm is None or resource_name != self._plan.litellm.service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "litellm"),
            resource_name=resource_name,
        )

    def create_litellm_service(self, resource_name: str) -> SharedCoreResourceRecord:
        if self._plan.litellm is None or resource_name != self._plan.litellm.service_name:
            raise SharedCoreError("Shared-core LiteLLM name does not match the active plan.")
        locator = self._ensure_compose_applied()
        return SharedCoreResourceRecord(
            resource_id=_resource_id(locator.compose_id, "litellm"),
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
                    return _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
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
                            compose_file=_render_compose_file(
                                self._plan,
                                self._mail_relay_config,
                                self._litellm_env,
                            ),
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
                    compose_file=_render_compose_file(
                        self._plan,
                        self._mail_relay_config,
                        self._litellm_env,
                    ),
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
                compose_file=_render_compose_file(
                    self._plan,
                    self._mail_relay_config,
                    self._litellm_env,
                ),
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


def _render_compose_file(
    plan: SharedCorePlan,
    mail_relay_config: dict[str, str],
    litellm_env: dict[str, str] | None = None,
) -> str:
    litellm_env = litellm_env or {}
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
    mail_block = ""
    if plan.mail_relay is not None:
        mail_volume = f"{plan.mail_relay.service_name}-spool"
        sender_domain = plan.mail_relay.from_address.split("@", 1)[1]
        mail_block = (
            f"  {plan.mail_relay.service_name}:\n"
            "    image: boky/postfix:latest\n"
            "    restart: unless-stopped\n"
            "    user: '0:0'\n"
            "    environment:\n"
            f"      ALLOWED_SENDER_DOMAINS: {sender_domain}\n"
            f"      POSTFIX_myhostname: {plan.mail_relay.mail_hostname}\n"
            "      POSTFIX_mynetworks: 0.0.0.0/0\n"
            f"    volumes:\n      - {mail_volume}:/var/spool/postfix\n"
            "    expose:\n"
            f"      - '{plan.mail_relay.smtp_port}'\n"
            "    networks:\n"
            "      - shared\n"
        )
        volume_block += f"  {mail_volume}:\n"
    litellm_block = ""
    config_block = ""
    if plan.litellm is not None:
        litellm_image = litellm_env.get("LITELLM_IMAGE", "ghcr.io/berriai/litellm").strip() or "ghcr.io/berriai/litellm"
        litellm_tag = litellm_env.get("LITELLM_IMAGE_TAG", "main-v1.40.14-stable").strip() or "main-v1.40.14-stable"
        litellm_config = render_litellm_config_yaml(
            build_litellm_config(litellm_env, _build_litellm_upstream_creds(litellm_env))
        )
        postgres_service_name = (
            plan.postgres.service_name if plan.postgres is not None else "shared-postgres"
        )
        postgres_password_env = _compose_env_var_name(plan.litellm.postgres.password_secret_ref)
        config_name = f"{plan.litellm.service_name}-config"
        litellm_block = (
            f"  {plan.litellm.service_name}:\n"
            f"    image: {litellm_image}:{litellm_tag}\n"
            "    restart: unless-stopped\n"
            "    command: [\"--config\", \"/app/config.yaml\", \"--port\", \"4000\"]\n"
            "    environment:\n"
            f'      DATABASE_URL: "postgresql://{plan.litellm.postgres.user_name}:${{{postgres_password_env}:-change-me}}@{postgres_service_name}:5432/{plan.litellm.postgres.database_name}"\n'
            '      MASTER_KEY: "${LITELLM_MASTER_KEY}"\n'
            '      SALT_KEY: "${LITELLM_SALT_KEY}"\n'
            "    configs:\n"
            f"      - source: {config_name}\n"
            "        target: /app/config.yaml\n"
            "    expose:\n"
            '      - "4000"\n'
            "    healthcheck:\n"
            '      test: ["CMD-SHELL", "python -c \'import urllib.request; urllib.request.urlopen(\\\"http://127.0.0.1:4000/health/liveliness\\\", timeout=5)\'"]\n'
            "      interval: 30s\n"
            "      timeout: 5s\n"
            "      retries: 5\n"
            "      start_period: 15s\n"
            "    networks:\n"
            "      shared:\n"
            "        aliases:\n"
            f"          - {plan.litellm.service_name}\n"
        )
        config_block = (
            "configs:\n"
            f"  {config_name}:\n"
            "    content: |\n"
            f"{_indent_block(litellm_config, 6)}"
        )
    return (
        "services:\n"
        f"{postgres_block}"
        f"{redis_block}"
        f"{mail_block}"
        f"{litellm_block}"
        "networks:\n"
        "  shared:\n"
        f"    name: {plan.network_name}\n"
        "volumes:\n"
        f"{volume_block or '  {}\n'}"
        f"{config_block}"
    )


def _build_litellm_upstream_creds(litellm_env: dict[str, str]) -> dict[str, str]:
    upstream_creds = {"opencode_go_api_key_env": "OPENCODE_GO_API_KEY"}
    openrouter_env_name = _first_configured_env_name(
        litellm_env,
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
        "OPENCLAW_OPENROUTER_API_KEY",
        "AI_DEFAULT_API_KEY",
        "OPENROUTER_API_KEY",
    )
    if openrouter_env_name is not None:
        upstream_creds["openrouter_api_key_env"] = openrouter_env_name
    nvidia_env_name = _first_configured_env_name(
        litellm_env,
        "MY_FARM_ADVISOR_NVIDIA_API_KEY",
        "OPENCLAW_NVIDIA_API_KEY",
        "NVIDIA_API_KEY",
    )
    if nvidia_env_name is not None:
        upstream_creds["nvidia_api_key_env"] = nvidia_env_name
    return upstream_creds


def _first_configured_env_name(litellm_env: dict[str, str], *candidates: str) -> str | None:
    for candidate in candidates:
        value = litellm_env.get(candidate)
        if value is not None and value.strip() != "":
            return candidate
    return None


def _compose_env_var_name(secret_ref: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in secret_ref).upper()


def _indent_block(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "".join(f"{prefix}{line}\n" for line in text.rstrip("\n").splitlines())


def _find_container_name(service_name: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--format",
            '{{.Names}}\t{{.Label "com.docker.compose.service"}}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        name, _, compose_service = line.partition("\t")
        if compose_service == service_name:
            return name
    return None


def _wait_for_container_name(
    service_name: str, *, attempts: int = 20, delay_seconds: float = 3.0
) -> str | None:
    for attempt in range(attempts):
        container_name = _find_container_name(service_name)
        if container_name is not None:
            return container_name
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _wait_for_postgres_ready(
    container_name: str, *, attempts: int = 20, delay_seconds: float = 3.0
) -> None:
    result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    for attempt in range(attempts):
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "sh",
                "-lc",
                'PGPASSWORD="$POSTGRES_PASSWORD" pg_isready -h 127.0.0.1 -U postgres -d postgres',
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    detail = (result.stderr or result.stdout).strip()
    raise SharedCoreError(
        "Shared-core Postgres did not become ready for allocation provisioning: "
        f"{detail or 'unknown error'}"
    )


def _postgres_password_for_allocation(allocation: SharedPostgresAllocation) -> str:
    del allocation
    return "change-me"


def _ensure_postgres_role(container_name: str, user_name: str, password: str) -> None:
    exists = _run_psql_scalar(
        container_name,
        f"SELECT 1 FROM pg_roles WHERE rolname = '{_sql_literal(user_name)}';",
    )
    if exists == "1":
        _run_psql(
            container_name,
            f'ALTER ROLE "{_sql_ident(user_name)}" '
            f"WITH LOGIN PASSWORD '{_sql_literal(password)}';",
        )
        return
    _run_psql(
        container_name,
        f"CREATE ROLE \"{_sql_ident(user_name)}\" WITH LOGIN PASSWORD '{_sql_literal(password)}';",
    )


def _ensure_postgres_database(container_name: str, database_name: str, owner_name: str) -> None:
    exists = _run_psql_scalar(
        container_name,
        f"SELECT 1 FROM pg_database WHERE datname = '{_sql_literal(database_name)}';",
    )
    if exists != "1":
        _run_psql(
            container_name,
            f'CREATE DATABASE "{_sql_ident(database_name)}" OWNER "{_sql_ident(owner_name)}";',
        )
        return
    _run_psql(
        container_name,
        f'ALTER DATABASE "{_sql_ident(database_name)}" OWNER TO "{_sql_ident(owner_name)}";',
    )
    _run_psql(
        container_name,
        f'GRANT ALL PRIVILEGES ON DATABASE "{_sql_ident(database_name)}" '
        f'TO "{_sql_ident(owner_name)}";',
    )


def _can_connect_as_allocation(container_name: str, allocation: SharedPostgresAllocation) -> bool:
    password = _postgres_password_for_allocation(allocation)
    shell = (
        f"PGPASSWORD={shlex.quote(password)} "
        "psql -h 127.0.0.1 "
        f"-U {shlex.quote(allocation.user_name)} "
        f"-d {shlex.quote(allocation.database_name)} "
        "-v ON_ERROR_STOP=1 -tAc 'SELECT 1'"
    )
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", shell],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def _run_psql_scalar(container_name: str, sql: str) -> str:
    result = _run_psql(container_name, sql)
    return result.stdout.strip()


def _run_psql(container_name: str, sql: str) -> subprocess.CompletedProcess[str]:
    shell = (
        'PGPASSWORD="$POSTGRES_PASSWORD" '
        "psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 "
        f"-tAc {shlex.quote(sql)}"
    )
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", shell],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SharedCoreError(
            f"Shared-core Postgres provisioning failed: {detail or 'unknown error'}"
        )
    return result


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _sql_ident(value: str) -> str:
    return value.replace('"', '""')
