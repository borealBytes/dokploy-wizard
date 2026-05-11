# ruff: noqa: E501
"""Dokploy-backed shared-core backend using a compose-first deployment flow."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from dokploy_wizard.core import (
    SharedCoreError,
    SharedCorePlan,
    SharedCoreResourceRecord,
    SharedPostgresAllocation,
)
from dokploy_wizard.dokploy.client import (
    DokployAiProvider,
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.container_resolution import resolve_compose_container_name
from dokploy_wizard.litellm import (
    LiteLLMAdminApi,
    LiteLLMGatewayManager,
    build_litellm_config,
    render_litellm_config_yaml,
)
from dokploy_wizard.litellm.model_catalog import (
    DEFAULT_LOCAL_CANONICAL_ALIAS,
    DEFAULT_LOCAL_UPSTREAM_TARGET,
    ModelCatalog,
    ModelCostMetadata,
    build_model_catalog,
)
from dokploy_wizard.state import write_litellm_generated_keys
from dokploy_wizard.state.models import LiteLLMGeneratedKeys


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


@runtime_checkable
class DokployAiProviderApi(Protocol):
    def ai_providers_all(self) -> tuple[DokployAiProvider, ...]: ...

    def ai_provider_create(
        self,
        *,
        name: str,
        api_url: str,
        api_key: str,
        model: str,
        is_enabled: bool,
    ) -> DokployAiProvider: ...

    def ai_provider_update(
        self,
        *,
        ai_id: str,
        name: str,
        api_url: str,
        api_key: str,
        model: str,
        is_enabled: bool,
    ) -> DokployAiProvider: ...


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
        litellm_generated_keys: LiteLLMGeneratedKeys | None = None,
        litellm_consumer_model_allowlists: dict[str, tuple[str, ...]] | None = None,
        litellm_admin_api: LiteLLMAdminApi | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        state_dir: Path = Path(".dokploy-wizard-state"),
    ) -> None:
        self._stack_name = stack_name
        self._plan = plan
        self._compose_name = plan.network_name
        self._mail_relay_config = mail_relay_config or {}
        self._litellm_env = litellm_env or {}
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False
        self._allocation_provisioner = allocation_provisioner
        self._litellm_generated_keys = litellm_generated_keys
        self._litellm_consumer_model_allowlists = litellm_consumer_model_allowlists or {}
        self._litellm_admin_api = litellm_admin_api
        self._sleep_fn = sleep_fn
        self._state_dir = state_dir

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

    def refresh_compose(self) -> None:
        self._ensure_compose_applied()
        self._wait_for_shared_core_containers()

    def reconcile_litellm_runtime(self) -> None:
        self._ensure_litellm_runtime_ready_and_reconciled()

    def _wait_for_shared_core_containers(
        self, *, attempts: int = 30, delay_seconds: float = 2.0
    ) -> None:
        services_to_wait = [self._plan.network_name]
        if self._plan.postgres is not None:
            services_to_wait.append(self._plan.postgres.service_name)
        if self._plan.redis is not None:
            services_to_wait.append(self._plan.redis.service_name)
        if self._plan.litellm is not None:
            services_to_wait.append(self._plan.litellm.service_name)
        if self._plan.mail_relay is not None:
            services_to_wait.append(self._plan.mail_relay.service_name)
        for service_name in services_to_wait:
            _wait_for_container_name(
                service_name, attempts=attempts, delay_seconds=delay_seconds
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
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        rendered_compose = _render_compose_file(
            self._plan,
            self._mail_relay_config,
            self._litellm_env,
            self._litellm_generated_keys,
        )
        reconcile_title = "dokploy-wizard shared core reconcile"
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
                        result = apply_compose_noop_guard(
                            rendered_compose=rendered_compose,
                            service_key=self._compose_name,
                            state_dir=self._state_dir,
                            client=self._client,
                            locator=_ComposeLocator(
                                project_id=project.project_id,
                                environment_id=environment.environment_id,
                                compose_id=compose.compose_id,
                            ),
                            compose_id=compose.compose_id,
                            title=reconcile_title,
                            description="Update shared core compose app",
                            verify_current=self._shared_core_runtime_ready_for_noop,
                            locator_factory=lambda compose_id: _ComposeLocator(
                                project_id=project.project_id,
                                environment_id=environment.environment_id,
                                compose_id=compose_id,
                            ),
                        )
                        self._applied_locator = result.locator
                        self._created_in_process = result.status == "applied"
                        return result.locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file=rendered_compose,
                    app_name=self._compose_name,
                )
                deployment = self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title=reconcile_title,
                    description="Create shared core compose app",
                )
                if not deployment.success:
                    raise SharedCoreError(
                        "Dokploy deploy for shared core compose app did not report success."
                    )
                persist_compose_artifact_hash(
                    state_dir=self._state_dir,
                    service_key=self._compose_name,
                    rendered_compose=rendered_compose,
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
                compose_file=rendered_compose,
                app_name=self._compose_name,
            )
            deployment = self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title=reconcile_title,
                description="Create shared core compose app",
            )
            if not deployment.success:
                raise SharedCoreError(
                    "Dokploy deploy for shared core compose app did not report success."
                )
            persist_compose_artifact_hash(
                state_dir=self._state_dir,
                service_key=self._compose_name,
                rendered_compose=rendered_compose,
            )
        except DokployApiError as error:
            raise SharedCoreError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        self._created_in_process = True
        return locator

    def _ensure_litellm_runtime_ready_and_reconciled(self) -> None:
        if self._plan.litellm is None or self._litellm_generated_keys is None:
            return
        if self._litellm_admin_api is None:
            return
        manager = LiteLLMGatewayManager(api=self._litellm_admin_api, sleep_fn=self._sleep_fn)
        try:
            manager.wait_until_ready()
            reconciled = manager.reconcile_virtual_keys(
                generated_keys=self._litellm_generated_keys.virtual_keys,
                consumer_model_allowlists=self._litellm_consumer_model_allowlists,
            )
        except Exception as error:
            raise SharedCoreError(str(error)) from error
        updated_virtual_keys = dict(self._litellm_generated_keys.virtual_keys)
        changed = False
        for consumer, record in reconciled.items():
            if updated_virtual_keys.get(consumer) != record.key:
                updated_virtual_keys[consumer] = record.key
                changed = True
        if changed:
            self._litellm_generated_keys = LiteLLMGeneratedKeys(
                format_version=self._litellm_generated_keys.format_version,
                master_key=self._litellm_generated_keys.master_key,
                salt_key=self._litellm_generated_keys.salt_key,
                virtual_keys=updated_virtual_keys,
            )
            write_litellm_generated_keys(self._state_dir, self._litellm_generated_keys)
            if isinstance(self._client, DokployAiProviderApi):
                try:
                    _ensure_dokploy_ai_provider(
                        self._client,
                        litellm_service_name=self._plan.litellm.service_name,
                        generated_keys=self._litellm_generated_keys,
                        litellm_env=self._litellm_env,
                    )
                except DokployApiError:
                    pass

    def _shared_core_runtime_ready_for_noop(self) -> bool:
        postgres_allocations = tuple(
            allocation.postgres for allocation in self._plan.allocations if allocation.postgres is not None
        )
        if self._plan.postgres is not None and not self.validate_postgres_allocations(
            postgres_allocations
        ):
            return False
        if self._plan.redis is not None:
            container_name = _find_container_name(self._plan.redis.service_name)
            if container_name is None or not _redis_is_ready(container_name):
                return False
        return self._litellm_runtime_ready_for_noop()

    def _litellm_runtime_ready_for_noop(self) -> bool:
        if self._plan.litellm is None:
            return True
        if self._litellm_admin_api is None:
            return False
        try:
            LiteLLMGatewayManager(api=self._litellm_admin_api, sleep_fn=self._sleep_fn).wait_until_ready(
                attempts=3,
                delay_seconds=2.0,
            )
        except Exception:
            return False
        return True


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


def _dokploy_ai_model_alias(litellm_env: dict[str, str]) -> str:
    provider = litellm_env.get("AI_DEFAULT_PROVIDER")
    model = litellm_env.get("AI_DEFAULT_MODEL")
    if provider and model:
        provider = provider.strip()
        model = model.strip()
        if provider:
            if provider == "local":
                provider_alias, _, _ = DEFAULT_LOCAL_CANONICAL_ALIAS.partition("/")
                return f"{provider_alias}/{model}"
            if "." in provider:
                return f"{provider}/{model}"
    return DEFAULT_LOCAL_CANONICAL_ALIAS


def _ensure_dokploy_ai_provider(
    client: DokployAiProviderApi,
    *,
    litellm_service_name: str,
    generated_keys: LiteLLMGeneratedKeys | None,
    litellm_env: dict[str, str] | None = None,
) -> None:
    if generated_keys is None:
        return
    internal_url = f"http://{litellm_service_name}:4000/v1"
    name = "Dokploy Wizard LiteLLM"
    model = _dokploy_ai_model_alias(litellm_env or {})
    api_key = generated_keys.master_key
    existing = client.ai_providers_all()
    wizard_provider = None
    for provider in existing:
        if provider.name == name:
            wizard_provider = provider
            break
    if wizard_provider is None:
        client.ai_provider_create(
            name=name,
            api_url=internal_url,
            api_key=api_key,
            model=model,
            is_enabled=True,
        )
    else:
        client.ai_provider_update(
            ai_id=wizard_provider.ai_id,
            name=name,
            api_url=internal_url,
            api_key=api_key,
            model=model,
            is_enabled=True,
        )


def _render_compose_file(
    plan: SharedCorePlan,
    mail_relay_config: dict[str, str],
    litellm_env: dict[str, str] | None = None,
    litellm_generated_keys: LiteLLMGeneratedKeys | None = None,
) -> str:
    litellm_env = litellm_env or {}
    postgres_block = ""
    volume_block = ""
    config_entries: list[str] = []
    if plan.postgres is not None:
        postgres_volume = f"{plan.postgres.service_name}-data"
        postgres_allocations = [
            allocation.postgres
            for allocation in plan.allocations
            if allocation.postgres is not None
        ]
        if plan.litellm is not None:
            postgres_allocations.append(plan.litellm.postgres)
        postgres_config_mount_block = ""
        postgres_init_sql = _render_postgres_init_sql(tuple(postgres_allocations))
        if postgres_init_sql:
            postgres_init_config_name = f"{plan.postgres.service_name}-init"
            postgres_config_mount_block = (
                "    configs:\n"
                f"      - source: {postgres_init_config_name}\n"
                "        target: /docker-entrypoint-initdb.d/01-init.sql\n"
            )
            config_entries.append(
                f"  {postgres_init_config_name}:\n"
                "    content: |\n"
                f"{_indent_block(postgres_init_sql, 6)}"
            )
        postgres_block = (
            f"  {plan.postgres.service_name}:\n"
            "    image: postgres:16-alpine\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      POSTGRES_DB: postgres\n"
            "      POSTGRES_USER: postgres\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change-me}\n"
            f"    volumes:\n      - {postgres_volume}:/var/lib/postgresql/data\n"
            f"{postgres_config_mount_block}"
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
            "    environment:\n"
            "      REDIS_PASSWORD: ${REDIS_PASSWORD:-change-me}\n"
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
    if plan.litellm is not None:
        litellm_image = litellm_env.get("LITELLM_IMAGE", "ghcr.io/berriai/litellm").strip() or "ghcr.io/berriai/litellm"
        litellm_tag = litellm_env.get("LITELLM_IMAGE_TAG", "v1.83.14-stable").strip() or "v1.83.14-stable"
        upstream_creds = _build_litellm_upstream_creds(litellm_env)
        litellm_config_payload = build_litellm_config(litellm_env, upstream_creds)
        _inline_litellm_secret_refs(litellm_config_payload, litellm_env)
        litellm_config = render_litellm_config_yaml(litellm_config_payload)
        postgres_service_name = (
            plan.postgres.service_name if plan.postgres is not None else "shared-postgres"
        )
        postgres_password_env = _compose_env_var_name(plan.litellm.postgres.password_secret_ref)
        config_name = _config_name_with_hash(
            f"{plan.litellm.service_name}-config", litellm_config
        )
        provider_env_lines = _render_litellm_upstream_env_lines(
            litellm_env=litellm_env, upstream_creds=upstream_creds
        )
        master_key_lines = (
            '      LITELLM_MASTER_KEY: "${LITELLM_MASTER_KEY}"\n'
            '      MASTER_KEY: "${LITELLM_MASTER_KEY}"\n'
        )
        salt_key_lines = (
            '      LITELLM_SALT_KEY: "${LITELLM_SALT_KEY}"\n'
            '      SALT_KEY: "${LITELLM_SALT_KEY}"\n'
        )
        if litellm_generated_keys is not None:
            master_key_lines = (
                f'      LITELLM_MASTER_KEY: "{litellm_generated_keys.master_key}"\n'
                f'      MASTER_KEY: "{litellm_generated_keys.master_key}"\n'
            )
            salt_key_lines = (
                f'      LITELLM_SALT_KEY: "{litellm_generated_keys.salt_key}"\n'
                f'      SALT_KEY: "{litellm_generated_keys.salt_key}"\n'
            )
        litellm_block = (
            f"  {plan.litellm.service_name}:\n"
            f"    image: {litellm_image}:{litellm_tag}\n"
            "    restart: unless-stopped\n"
            "    command: [\"--config\", \"/app/config.yaml\", \"--port\", \"4000\"]\n"
            "    environment:\n"
            f'      DATABASE_URL: "postgresql://{plan.litellm.postgres.user_name}:${{{postgres_password_env}:-change-me}}@{postgres_service_name}:5432/{plan.litellm.postgres.database_name}"\n'
            f"{provider_env_lines}"
            f"{master_key_lines}"
            f"{salt_key_lines}"
            "    configs:\n"
            f"      - source: {config_name}\n"
            "        target: /app/config.yaml\n"
            "    ports:\n"
            '      - "127.0.0.1:4000:4000"\n'
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
        config_entries.append(
            f"  {config_name}:\n"
            "    content: |\n"
            f"{_indent_block(litellm_config, 6)}"
        )
    config_block = f"configs:\n{''.join(config_entries)}" if config_entries else ""
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
    upstream_creds: dict[str, str] = {}
    if _first_configured_env_name(
        litellm_env, *_litellm_secret_ref_candidates("LITELLM_OPENCODE_GO_API_KEY")
    ) is not None:
        upstream_creds["opencode_go_api_key_env"] = "LITELLM_OPENCODE_GO_API_KEY"
    if _first_configured_env_name(
        litellm_env, *_litellm_secret_ref_candidates("LITELLM_OPENROUTER_API_KEY")
    ) is not None:
        upstream_creds["openrouter_api_key_env"] = "LITELLM_OPENROUTER_API_KEY"
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


def _config_name_with_hash(base_name: str, content: str) -> str:
    return f"{base_name}-{sha256(content.encode('utf-8')).hexdigest()[:12]}"


def _render_litellm_upstream_env_lines(
    *, litellm_env: dict[str, str], upstream_creds: dict[str, str]
) -> str:
    lines: list[str] = []
    rendered_names: set[str] = set()
    for env_name in upstream_creds.values():
        if env_name in rendered_names:
            continue
        source_env_name = _first_configured_env_name(
            litellm_env, *_litellm_secret_ref_candidates(env_name)
        )
        if source_env_name is None:
            continue
        lines.append(f'      {env_name}: "${{{source_env_name}}}"\n')
        rendered_names.add(env_name)
    return "".join(lines)


def _litellm_secret_ref_candidates(env_name: str) -> tuple[str, ...]:
    if env_name == "LITELLM_OPENCODE_GO_API_KEY":
        return ("LITELLM_OPENCODE_GO_API_KEY", "OPENCODE_GO_API_KEY")
    if env_name == "LITELLM_OPENROUTER_API_KEY":
        return (
            "LITELLM_OPENROUTER_API_KEY",
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
            "OPENCLAW_OPENROUTER_API_KEY",
            "AI_DEFAULT_API_KEY",
            "OPENROUTER_API_KEY",
        )
    return (env_name,)


def _resolve_litellm_secret_ref_value(
    litellm_env: dict[str, str], env_name: str
) -> str | None:
    source_env_name = _first_configured_env_name(
        litellm_env, *_litellm_secret_ref_candidates(env_name)
    )
    if source_env_name is None:
        return None
    value = litellm_env.get(source_env_name)
    if value is None or value.strip() == "":
        return None
    return value


def _inline_litellm_secret_refs(config: dict[str, object], litellm_env: dict[str, str]) -> None:
    model_list = config.get("model_list")
    if not isinstance(model_list, list):
        return
    for entry in model_list:
        if not isinstance(entry, dict):
            continue
        litellm_params = entry.get("litellm_params")
        if not isinstance(litellm_params, dict):
            continue
        api_key_ref = litellm_params.get("api_key")
        if not isinstance(api_key_ref, str) or not api_key_ref.startswith("os.environ/"):
            continue
        env_name = api_key_ref.removeprefix("os.environ/")
        value = _resolve_litellm_secret_ref_value(litellm_env, env_name)
        if value is None:
            continue
        litellm_params["api_key"] = value


def build_litellm_consumer_model_allowlists(
    *,
    flat_env: dict[str, str],
    plan: SharedCorePlan,
) -> dict[str, tuple[str, ...]]:
    if plan.litellm is None:
        return {}
    config = build_litellm_config(flat_env, _build_litellm_upstream_creds(flat_env))
    projection_catalog = _build_litellm_consumer_projection_catalog(
        flat_env=flat_env,
        config=config,
        plan=plan,
    )
    return {
        consumer: projection_catalog.fallback_alias_order_for(consumer)
        for consumer in _litellm_consumers()
    }


def _build_litellm_consumer_projection_catalog(
    *,
    flat_env: dict[str, str],
    config: Mapping[str, object],
    plan: SharedCorePlan,
) -> ModelCatalog:
    litellm_plan = plan.litellm
    if litellm_plan is None:
        raise ValueError("LiteLLM consumer projection catalog requires an active LiteLLM plan.")
    base_catalog = _build_catalog_from_litellm_config(config)
    default_aliases = _resolve_litellm_default_aliases(
        aliases=litellm_plan.default_model_alias_order,
        catalog=base_catalog,
    )
    shared_aliases = tuple(
        dict.fromkeys(
            [
                *default_aliases,
                *(entry.alias for entry in base_catalog.entries if entry.provider_slug == "opencode-go"),
                *(entry.alias for entry in base_catalog.entries if entry.provider_slug == "openrouter"),
            ]
        )
    )
    visible_aliases_by_consumer = {
        consumer: shared_aliases for consumer in _litellm_consumers()
    }
    visible_aliases_by_consumer["my-farm-advisor"] = _advisor_alias_allowlist(
        flat_env,
        primary_key="MY_FARM_ADVISOR_PRIMARY_MODEL",
        fallback_key="MY_FARM_ADVISOR_FALLBACK_MODELS",
        catalog=base_catalog,
        default_aliases=shared_aliases,
    )
    visible_aliases_by_consumer["openclaw"] = _advisor_alias_allowlist(
        flat_env,
        primary_key="OPENCLAW_PRIMARY_MODEL",
        fallback_key="OPENCLAW_FALLBACK_MODELS",
        catalog=base_catalog,
        default_aliases=shared_aliases,
    )
    return _project_catalog(base_catalog, visible_aliases_by_consumer)


def _advisor_alias_allowlist(
    flat_env: dict[str, str],
    *,
    primary_key: str,
    fallback_key: str,
    catalog: ModelCatalog,
    default_aliases: tuple[str, ...],
) -> tuple[str, ...]:
    aliases = list(default_aliases)
    primary_model = _optional_value(flat_env, primary_key)
    resolved_primary = _resolve_requested_catalog_alias(primary_model, catalog)
    if resolved_primary is not None:
        aliases.append(resolved_primary)
    fallback_models = _optional_value(flat_env, fallback_key)
    if fallback_models is not None:
        for model in fallback_models.split(","):
            resolved_alias = _resolve_requested_catalog_alias(model.strip(), catalog)
            if resolved_alias is not None:
                aliases.append(resolved_alias)
    return tuple(dict.fromkeys(aliases))


def _build_catalog_from_litellm_config(config: Mapping[str, object]) -> ModelCatalog:
    model_entries = config.get("model_list")
    if not isinstance(model_entries, list):
        raise ValueError("LiteLLM config model_list must be a list.")
    local_alias = DEFAULT_LOCAL_CANONICAL_ALIAS
    local_upstream_target = DEFAULT_LOCAL_UPSTREAM_TARGET
    openrouter_model_ids: list[str] = []
    opencode_go_model_ids: list[str] = []
    passthrough_alias_targets: dict[str, str] = {}
    cost_metadata_by_alias: dict[str, ModelCostMetadata] = {}
    default_alias_order: list[str] = []

    for entry in model_entries:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("model_name")
        litellm_params = entry.get("litellm_params")
        if not isinstance(alias, str) or not isinstance(litellm_params, dict):
            continue
        upstream_target = litellm_params.get("model")
        if not isinstance(upstream_target, str):
            continue
        if _is_local_alias(alias):
            local_alias = alias
            local_upstream_target = upstream_target
        elif alias.startswith("openrouter/"):
            openrouter_model_ids.append(alias.removeprefix("openrouter/"))
        elif alias.startswith("opencode-go/"):
            opencode_go_model_ids.append(alias.removeprefix("opencode-go/"))
        else:
            passthrough_alias_targets[alias] = upstream_target

        default_alias_order.append(alias)

        model_info = entry.get("model_info")
        if isinstance(model_info, dict):
            input_cost = model_info.get("input_cost_per_token")
            output_cost = model_info.get("output_cost_per_token")
            if isinstance(input_cost, (int, float)) or isinstance(output_cost, (int, float)):
                cost_metadata_by_alias[alias] = ModelCostMetadata(
                    input_cost_per_token=float(input_cost)
                    if isinstance(input_cost, (int, float))
                    else None,
                    output_cost_per_token=float(output_cost)
                    if isinstance(output_cost, (int, float))
                    else None,
                )

    return build_model_catalog(
        local_alias=local_alias,
        local_upstream_target=local_upstream_target,
        openrouter_model_ids=tuple(openrouter_model_ids),
        opencode_go_model_ids=tuple(opencode_go_model_ids),
        nvidia_alias_targets=passthrough_alias_targets,
        default_alias_order=tuple(default_alias_order),
        cost_metadata_by_alias=cost_metadata_by_alias,
    )


def _is_local_alias(alias: str) -> bool:
    provider_slug, _, _ = alias.partition("/")
    default_local_provider, _, _ = DEFAULT_LOCAL_CANONICAL_ALIAS.partition("/")
    return provider_slug == default_local_provider or "." in provider_slug


def _project_catalog(
    base_catalog: ModelCatalog,
    visible_aliases_by_consumer: Mapping[str, tuple[str, ...]],
) -> ModelCatalog:
    passthrough_alias_targets: dict[str, str] = {}
    openrouter_model_ids: list[str] = []
    opencode_go_model_ids: list[str] = []
    cost_metadata_by_alias: dict[str, ModelCostMetadata] = {}
    local_entry = next((entry for entry in base_catalog.entries if _is_local_alias(entry.alias)), None)
    if local_entry is None:
        local_entry = base_catalog.entry_for(DEFAULT_LOCAL_CANONICAL_ALIAS)

    for entry in base_catalog.entries:
        if entry.alias == local_entry.alias:
            continue
        if entry.provider_slug == "openrouter":
            openrouter_model_ids.append(entry.model_id)
        elif entry.provider_slug == "opencode-go":
            opencode_go_model_ids.append(entry.model_id)
        else:
            passthrough_alias_targets[entry.alias] = entry.upstream_target
        if entry.input_cost_per_token is not None or entry.output_cost_per_token is not None:
            cost_metadata_by_alias[entry.alias] = ModelCostMetadata(
                input_cost_per_token=entry.input_cost_per_token,
                output_cost_per_token=entry.output_cost_per_token,
            )

    return build_model_catalog(
        local_alias=local_entry.alias,
        local_upstream_target=local_entry.upstream_target,
        openrouter_model_ids=tuple(openrouter_model_ids),
        opencode_go_model_ids=tuple(opencode_go_model_ids),
        nvidia_alias_targets=passthrough_alias_targets,
        visible_aliases_by_consumer=visible_aliases_by_consumer,
        default_alias_order=tuple(entry.alias for entry in base_catalog.entries),
        cost_metadata_by_alias=cost_metadata_by_alias,
    )


def _resolve_litellm_default_aliases(
    *,
    aliases: tuple[str, ...],
    catalog: ModelCatalog,
) -> tuple[str, ...]:
    resolved: list[str] = []
    for alias in aliases:
        for projected_alias in _project_catalog_alias(alias, catalog):
            if projected_alias not in resolved:
                resolved.append(projected_alias)
    return tuple(resolved)


def _project_catalog_alias(alias: str, catalog: ModelCatalog) -> tuple[str, ...]:
    if alias.endswith("/*"):
        provider_slug = alias.removesuffix("/*")
        return tuple(
            entry.alias for entry in catalog.entries if entry.provider_slug == provider_slug
        )
    resolved_alias = _resolve_requested_catalog_alias(alias, catalog)
    return (resolved_alias,) if resolved_alias is not None else ()


def _resolve_requested_catalog_alias(model_ref: str | None, catalog: ModelCatalog) -> str | None:
    if model_ref is None:
        return None
    alias_lookup = _catalog_alias_lookup(catalog)
    if model_ref in alias_lookup:
        return alias_lookup[model_ref]
    legacy_local_ref = model_ref.removeprefix("local/") if model_ref.startswith("local/") else None
    if legacy_local_ref is not None:
        return alias_lookup.get(legacy_local_ref)
    return None


def _catalog_alias_lookup(catalog: ModelCatalog) -> dict[str, str]:
    alias_lookup = {entry.alias: entry.alias for entry in catalog.entries}
    aliases_by_model_id: dict[str, list[str]] = {}
    for entry in catalog.entries:
        aliases_by_model_id.setdefault(entry.model_id, []).append(entry.alias)
    for model_id, matching_aliases in aliases_by_model_id.items():
        if len(matching_aliases) == 1:
            alias_lookup[model_id] = matching_aliases[0]
    return alias_lookup


def _litellm_consumers() -> tuple[str, ...]:
    return ("coder-hermes", "coder-kdense", "my-farm-advisor", "openclaw")


def _optional_value(flat_env: dict[str, str], key: str) -> str | None:
    value = flat_env.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _compose_env_var_name(secret_ref: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in secret_ref).upper()


def _render_postgres_init_sql(allocations: tuple[SharedPostgresAllocation, ...]) -> str:
    statements: list[str] = []
    for allocation in allocations:
        user_name = _quote_postgres_identifier(allocation.user_name)
        database_name = _quote_postgres_identifier(allocation.database_name)
        password = _quote_postgres_literal(_postgres_password_for_allocation(allocation))
        statements.extend(
            (
                f"CREATE ROLE {user_name} WITH LOGIN PASSWORD {password};",
                f"CREATE DATABASE {database_name} OWNER {user_name};",
                f"GRANT ALL PRIVILEGES ON DATABASE {database_name} TO {user_name};",
                "",
            )
        )
    return "\n".join(statements).rstrip()


def _quote_postgres_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_postgres_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _indent_block(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "".join(f"{prefix}{line}\n" for line in text.rstrip("\n").splitlines())


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
    return resolve_compose_container_name(service_name, result.stdout.splitlines())


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


def _redis_is_ready(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", 'redis-cli -a "$REDIS_PASSWORD" ping'],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "PONG"


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
