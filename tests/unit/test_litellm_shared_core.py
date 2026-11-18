# ruff: noqa: E501

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

import dokploy_wizard.dokploy.shared_core as shared_core_module
from dokploy_wizard.core.models import SharedCoreResourceRecord, SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.core.reconciler import build_shared_core_ledger, reconcile_shared_core
from dokploy_wizard.dokploy.client import DokployAiProvider
from dokploy_wizard.dokploy.env_spec import RenderedCompose
from dokploy_wizard.dokploy.shared_core import (
    DokploySharedCoreBackend,
    _render_compose_file,
    build_litellm_consumer_model_allowlists,
)
from dokploy_wizard.litellm.admin import LiteLLMTeamRecord, LiteLLMVirtualKeyRecord
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    resolve_desired_state,
    write_applied_checkpoint,
)
from dokploy_wizard.state.models import (
    STATE_FORMAT_VERSION,
    LiteLLMGeneratedKeys,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
)

from .fake_dokploy import FakeDokployApiClient


class _RecordingSharedCoreBackend:
    def __init__(self) -> None:
        self.ensured_allocations: tuple[SharedPostgresAllocation, ...] = ()
        self._records: dict[str, SharedCoreResourceRecord] = {}

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._get_existing(resource_id)

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return self._find_existing(resource_name)

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        return self._record(resource_name)

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._get_existing(resource_id)

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return self._find_existing(resource_name)

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return self._record(resource_name)

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._get_existing(resource_id)

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return self._find_existing(resource_name)

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return self._record(resource_name)

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._get_existing(resource_id)

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return self._find_existing(resource_name)

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return self._record(resource_name)

    def get_litellm_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._get_existing(resource_id)

    def find_litellm_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return self._find_existing(resource_name)

    def create_litellm_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return self._record(resource_name)

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        self.ensured_allocations = allocations

    def refresh_compose(self) -> None:
        return None

    def reconcile_litellm_runtime(self) -> None:
        return None

    def _record(self, resource_name: str) -> SharedCoreResourceRecord:
        record = SharedCoreResourceRecord(
            resource_id=f"resource-{resource_name}",
            resource_name=resource_name,
        )
        self._records[record.resource_id] = record
        return record

    def _get_existing(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return self._records.get(resource_id)

    def _find_existing(self, resource_name: str) -> SharedCoreResourceRecord | None:
        for record in self._records.values():
            if record.resource_name == resource_name:
                return record
        return None


def test_litellm_plan_exists_without_ai_packs() -> None:
    plan = build_shared_core_plan(stack_name="openmerge", enabled_packs=())

    assert plan.network_name == "openmerge-shared"
    assert plan.postgres is not None
    assert plan.postgres.service_name == "openmerge-shared-postgres"
    assert plan.redis is None
    assert plan.allocations == ()
    assert plan.litellm is not None
    assert plan.litellm.service_name == "openmerge-shared-litellm"
    assert plan.litellm.postgres == SharedPostgresAllocation(
        database_name="openmerge_litellm",
        user_name="openmerge_litellm",
        password_secret_ref="openmerge-litellm-postgres-password",
    )
    assert plan.litellm.default_model_alias_order == ("opencode-go/deepseek-v4.1-flash",)


def test_litellm_plan_uses_openrouter_default_without_local_upstream() -> None:
    values = {
        "AI_DEFAULT_PROVIDER": "openrouter",
        "AI_DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash:free",
        "LITELLM_OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
        "LITELLM_OPENROUTER_MODELS": "deepseek/deepseek-v4.1-flash:free",
    }

    plan = build_shared_core_plan(
        stack_name="openmerge",
        enabled_packs=("coder",),
        values=values,
    )
    allowlists = build_litellm_consumer_model_allowlists(flat_env=values, plan=plan)

    assert plan.litellm is not None
    assert plan.litellm.default_model_alias_order == (
        "openrouter/deepseek/deepseek-v4.1-flash:free",
    )
    for aliases in allowlists.values():
        assert "local-model.internal/unsloth-active" not in aliases
    assert allowlists == {
        "coder-hermes": ("openrouter/deepseek/deepseek-v4.1-flash:free",),
        "coder-workspace-issuer": ("*",),
        "dokploy-ai": ("openrouter/deepseek/deepseek-v4.1-flash:free",),
        "nextcloud": ("openrouter/deepseek/deepseek-v4.1-flash:free",),
    }


def test_litellm_plan_keeps_local_default_when_local_upstream_is_active() -> None:
    values = {
        "AI_DEFAULT_PROVIDER": "local-model.internal",
        "AI_DEFAULT_MODEL": "unsloth-active",
        "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
        "LITELLM_LOCAL_API_KEY": "SECRET_TEST_LOCAL_PROVIDER_KEY",
    }

    plan = build_shared_core_plan(
        stack_name="openmerge",
        enabled_packs=("coder",),
        values=values,
    )
    allowlists = build_litellm_consumer_model_allowlists(flat_env=values, plan=plan)

    assert plan.litellm is not None
    assert plan.litellm.default_model_alias_order == (
        "local-model.internal/unsloth-active",
    )
    assert allowlists == {
        "coder-hermes": ("local-model.internal/unsloth-active",),
        "coder-workspace-issuer": ("*",),
        "dokploy-ai": ("local-model.internal/unsloth-active",),
        "nextcloud": ("local-model.internal/unsloth-active",),
    }


def test_litellm_db_allocation_is_dedicated_and_not_a_pack_allocation() -> None:
    plan = build_shared_core_plan(stack_name="openmerge", enabled_packs=("moodle", "nextcloud"))

    assert plan.litellm is not None
    assert [allocation.pack_name for allocation in plan.allocations] == ["moodle", "nextcloud"]
    assert all(allocation.pack_name != "litellm" for allocation in plan.allocations)
    assert all(allocation.postgres != plan.litellm.postgres for allocation in plan.allocations)
    assert plan.litellm.postgres == SharedPostgresAllocation(
        database_name="openmerge_litellm",
        user_name="openmerge_litellm",
        password_secret_ref="openmerge-litellm-postgres-password",
    )


def test_reconcile_repairs_litellm_postgres_allocation_without_pack_allocations() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "example.com"},
        )
    )
    backend = _RecordingSharedCoreBackend()

    reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert desired_state.shared_core.allocations == ()
    assert desired_state.shared_core.litellm is not None
    assert backend.ensured_allocations == (desired_state.shared_core.litellm.postgres,)


def test_reconcile_repairs_pack_and_litellm_postgres_allocations() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = _RecordingSharedCoreBackend()

    reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert desired_state.shared_core.litellm is not None
    assert backend.ensured_allocations == (
        *(allocation.postgres for allocation in desired_state.shared_core.allocations if allocation.postgres is not None),
        desired_state.shared_core.litellm.postgres,
    )


def test_reconcile_reuses_desired_network_when_ledger_points_to_stale_name() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge-me",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "nextcloud",
            },
        )
    )
    backend = _RecordingSharedCoreBackend()
    stale_network = backend._record("wizard-stack-shared")
    desired_network = backend._record("openmerge-me-shared")
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                resource_type="shared_core_network",
                resource_id=stale_network.resource_id,
                scope="stack:openmerge-me:shared-network",
            ),
        ),
    )

    phase = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    updated_ledger = build_shared_core_ledger(
        existing_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        network_resource_id=phase.network_resource_id,
        postgres_resource_id=phase.postgres_resource_id,
        redis_resource_id=phase.redis_resource_id,
        mail_relay_resource_id=phase.mail_relay_resource_id,
        litellm_resource_id=phase.litellm_resource_id,
    )

    assert phase.result.network is not None
    assert phase.result.network.action == "reuse_existing"
    assert phase.result.network.resource_id == desired_network.resource_id
    assert phase.result.network.resource_name == "openmerge-me-shared"
    assert phase.network_resource_id == desired_network.resource_id
    assert next(
        resource
        for resource in updated_ledger.resources
        if resource.resource_type == "shared_core_network"
    ).resource_id == desired_network.resource_id


def test_rendered_compose_routes_the_admin_hostname_through_traefik() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {},
        litellm_admin_hostname="litellm.example.com",
    )
    compose = rendered.compose_file
    assert plan.litellm is not None
    service = plan.litellm.service_name

    # Traefik answers 404 for a hostname with no rule, and a proxy 404 looks exactly
    # like a dead service to anyone standing outside the box.
    assert '      traefik.enable: "true"\n' in compose
    assert f'      traefik.http.routers.{service}.rule: "Host(`litellm.example.com`)"\n' in compose
    assert f'      traefik.http.routers.{service}.entrypoints: "websecure"\n' in compose
    assert f'      traefik.http.routers.{service}.tls: "true"\n' in compose
    # The tunnel terminates TLS and talks plain HTTP to Traefik's listener, so the app
    # has to be told it is behind https or LiteLLM builds http:// redirects.
    assert (
        f"      traefik.http.middlewares.{service}-forwarded-https.headers"
        '.customrequestheaders.X-Forwarded-Proto: "https"\n' in compose
    )
    assert f'      traefik.http.services.{service}.loadbalancer.server.port: "4000"\n' in compose


def test_rendered_compose_publishes_no_route_without_an_admin_hostname() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    compose = _render_compose_file(plan, {}, {}).compose_file

    # No router is better than a router on a hostname nobody asked for.
    assert "traefik" not in compose


def test_rendered_compose_gives_the_litellm_admin_ui_the_stack_admin_credentials() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "stack-admin-password",
        },
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    # Same login as every other app on the box, rather than the master key.
    assert 'UI_USERNAME: "${LITELLM_UI_USERNAME:?LITELLM_UI_USERNAME is required}"' in compose
    assert 'UI_PASSWORD: "${LITELLM_UI_PASSWORD:?LITELLM_UI_PASSWORD is required}"' in compose
    assert env_specs["LITELLM_UI_USERNAME"].value == "admin@example.com"
    assert env_specs["LITELLM_UI_PASSWORD"].value == "stack-admin-password"
    assert env_specs["LITELLM_UI_PASSWORD"].variable.sensitive is True
    # The username is an email address, not a secret: it shows up in log lines and
    # operator notes, and marking it sensitive makes the deploy output useless.
    assert env_specs["LITELLM_UI_USERNAME"].variable.sensitive is False
    assert env_specs["LITELLM_UI_PASSWORD"].target_services == ("wizard-stack-shared-litellm",)


def test_rendered_compose_includes_pinned_litellm_service_and_provider_env_refs() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_IMAGE": "ghcr.io/berriai/litellm",
            "LITELLM_IMAGE_TAG": "main-v1.40.14-stable",
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini"
            ),
            "OPENROUTER_API_KEY": "legacy-openrouter-upstream-key",
        },
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    assert isinstance(rendered, RenderedCompose)
    assert "  wizard-stack-shared-litellm:\n" in compose
    assert "image: ghcr.io/berriai/litellm:main-v1.40.14-stable" in compose
    assert "image: ghcr.io/berriai/litellm:latest" not in compose
    assert 'DATABASE_URL: "postgresql://wizard_stack_litellm:${WIZARD_STACK_LITELLM_POSTGRES_PASSWORD:?WIZARD_STACK_LITELLM_POSTGRES_PASSWORD is required}@wizard-stack-shared-postgres:5432/wizard_stack_litellm"' in compose
    assert 'ENFORCE_PRISMA_MIGRATION_CHECK: "true"' in compose
    assert "ENFORCE_PRISMA_MIGRATION_CHECK" not in env_specs
    assert 'LITELLM_MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'LITELLM_SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert 'SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert "healthcheck:\n" in compose
    # Root serves Swagger; send operators to the admin UI and keep the docs reachable.
    assert 'ROOT_REDIRECT_URL: "/ui"' in compose
    assert 'DOCS_URL: "/docs"' in compose
    # No admin credentials in this env, so the UI login stays unset rather than
    # referencing placeholders that nothing would fill.
    assert "UI_USERNAME" not in compose
    assert "UI_PASSWORD" not in compose
    assert "LITELLM_UI_USERNAME" not in env_specs
    assert "LITELLM_UI_PASSWORD" not in env_specs
    assert re.search(r"source: wizard-stack-shared-litellm-config-[0-9a-f]{12}", compose)
    assert "target: /app/config.yaml" in compose
    assert 'api_key: "os.environ/LITELLM_LOCAL_API_KEY"' in compose
    assert 'model_name: "opencode-go/deepseek-v4.1-flash"' in compose
    assert 'model_name: "opencode-go/minimax-m2.7"' not in compose
    assert 'model_name: "openrouter/hunter-alpha"' in compose
    assert '      LITELLM_OPENCODE_GO_API_KEY: "${LITELLM_OPENCODE_GO_API_KEY:?LITELLM_OPENCODE_GO_API_KEY is required}"' in compose
    assert (
        '      LITELLM_OPENROUTER_API_KEY: "${LITELLM_OPENROUTER_API_KEY:?LITELLM_OPENROUTER_API_KEY is required}"'
        in compose
    )
    assert env_specs["LITELLM_OPENCODE_GO_API_KEY"].value == "opencode-go-upstream-key"
    assert env_specs["LITELLM_OPENROUTER_API_KEY"].value == "legacy-openrouter-upstream-key"
    assert env_specs["LITELLM_LOCAL_API_KEY"].target_services == ("wizard-stack-shared-litellm",)
    assert env_specs["LITELLM_MASTER_KEY"].target_services == (
        "wizard-stack-shared-litellm",
        "wizard-stack-shared-litellm-sync",
    )
    assert env_specs["LITELLM_OPENCODE_GO_API_KEY"].target_services == (
        "wizard-stack-shared-litellm",
        "wizard-stack-shared-litellm-sync",
    )
    assert env_specs["LITELLM_OPENROUTER_API_KEY"].target_services == (
        "wizard-stack-shared-litellm",
        "wizard-stack-shared-litellm-sync",
    )
    assert env_specs["LITELLM_SALT_KEY"].target_services == ("wizard-stack-shared-litellm",)
    assert env_specs["WIZARD_STACK_LITELLM_POSTGRES_PASSWORD"].target_services == (
        "wizard-stack-shared-postgres",
        "wizard-stack-shared-litellm",
    )
    assert "opencode-go-upstream-key" not in compose
    assert "legacy-openrouter-upstream-key" not in compose
    assert "    aliases:\n          - wizard-stack-shared-litellm\n" in compose
    assert '      - "127.0.0.1:4000:4000"' in compose
    assert "    expose:\n" not in compose


def test_rendered_litellm_local_model_disables_system_role_for_vllm_templates() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "AI_DEFAULT_PROVIDER": "local-model.internal",
            "AI_DEFAULT_MODEL": "unsloth-active",
            "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
            "LITELLM_OPENROUTER_API_KEY": "openrouter-upstream-key",
            "LITELLM_OPENROUTER_MODELS": "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini",
        },
    )
    compose = rendered.compose_file

    local_block = _embedded_litellm_model_block(
        compose,
        "local-model.internal/unsloth-active",
    )
    # A non-OpenCode-Go default provider means no static Go alias route at all;
    # the sidecar exposes the full Go catalog under sync/opencode-go/* instead.
    assert 'model_name: "opencode-go/deepseek-v4.1-flash"' not in compose
    openrouter_block = _embedded_litellm_model_block(compose, "openrouter/hunter-alpha")

    assert "supports_system_message: false" in local_block
    assert "supports_system_message" not in openrouter_block
    assert "SECRET" not in compose


def test_rendered_shared_postgres_pins_pgvector_image_without_logical_wal() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("nextcloud",))

    rendered = _render_compose_file(plan, {}, {})
    compose = rendered.compose_file

    assert "image: pgvector/pgvector:pg16" in compose
    assert 'command: ["postgres"]' in compose
    assert "wal_level=logical" not in compose
    # Pack roles are LOGIN-scoped to their own database only; the superuser and
    # extension-creation grants existed solely for a retired pack and must not
    # creep back into the shared Postgres init script.
    assert 'run_sql "ALTER ROLE "wizard_stack_nextcloud" WITH LOGIN PASSWORD' in compose
    assert "WITH SUPERUSER" not in compose
    assert "CREATE EXTENSION" not in compose


def test_rendered_compose_prefers_canonical_litellm_provider_env_refs() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "LITELLM_OPENCODE_GO_API_KEY": "litellm-opencode-key",
            "OPENCODE_GO_API_KEY": "legacy-opencode-go-key",
            "LITELLM_OPENROUTER_MODELS": "google/gemma-4-31b-it:free",
            "LITELLM_OPENROUTER_API_KEY": "litellm-openrouter-key",
            "OPENROUTER_API_KEY": "legacy-openrouter-key",
        },
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    assert '      LITELLM_OPENCODE_GO_API_KEY: "${LITELLM_OPENCODE_GO_API_KEY:?LITELLM_OPENCODE_GO_API_KEY is required}"' in compose
    assert '      LITELLM_OPENROUTER_API_KEY: "${LITELLM_OPENROUTER_API_KEY:?LITELLM_OPENROUTER_API_KEY is required}"' in compose
    assert '      LITELLM_OPENCODE_GO_API_KEY: "${OPENCODE_GO_API_KEY}' not in compose
    assert (
        '      LITELLM_OPENROUTER_API_KEY: "${OPENROUTER_API_KEY'
        not in compose
    )
    assert env_specs["LITELLM_OPENCODE_GO_API_KEY"].value == "litellm-opencode-key"
    assert env_specs["LITELLM_OPENROUTER_API_KEY"].value == "litellm-openrouter-key"
    assert 'model_name: "opencode-go/deepseek-v4.1-flash"' in compose
    assert 'model_name: "openrouter/google/gemma-4-31b-it:free"' in compose


def test_litellm_inline_config_name_changes_when_model_content_changes() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered_first = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        },
    )
    rendered_second = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "other-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        },
    )

    first_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})",
        rendered_first.compose_file,
    )
    second_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})",
        rendered_second.compose_file,
    )

    assert first_name is not None
    assert second_name is not None
    assert first_name.group(1) != second_name.group(1)


def test_litellm_inline_config_name_changes_when_openrouter_model_inventory_changes() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered_first = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_OPENROUTER_API_KEY": "litellm-openrouter-key",
            "LITELLM_OPENROUTER_MODELS": "minimax/minimax-m2.5:free",
        },
    )
    rendered_second = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_OPENROUTER_API_KEY": "litellm-openrouter-key",
            "LITELLM_OPENROUTER_MODELS": (
                "minimax/minimax-m2.5:free,google/gemma-4-31b-it:free"
            ),
        },
    )

    first_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})",
        rendered_first.compose_file,
    )
    second_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})",
        rendered_second.compose_file,
    )

    assert first_name is not None
    assert second_name is not None
    assert first_name.group(1) != second_name.group(1)


def test_rendered_compose_uses_env_specs_for_generated_litellm_keys() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {},
        litellm_generated_keys=_generated_keys(),
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    assert "sk-master-fake-test-key" not in compose
    assert "sk-salt-fake-test-key" not in compose
    assert 'LITELLM_MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'LITELLM_SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert 'SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert env_specs["LITELLM_MASTER_KEY"].value == "sk-master-fake-test-key"
    assert env_specs["LITELLM_SALT_KEY"].value == "sk-salt-fake-test-key"
    assert env_specs["LITELLM_VIRTUAL_KEY_CODER_HERMES"].value == "sk-hermes-fake-test-key"
    assert env_specs["LITELLM_VIRTUAL_KEY_CODER_HERMES"].required is False
    assert env_specs["LITELLM_VIRTUAL_KEY_CODER_KDENSE"].value == "sk-kdense-fake-test-key"
    assert env_specs["LITELLM_VIRTUAL_KEY_CODER_KDENSE"].required is False
    assert env_specs["LITELLM_VIRTUAL_KEY_DOKPLOY_AI"].value == "sk-dokploy-ai-fake-test-key"
    assert env_specs["LITELLM_VIRTUAL_KEY_DOKPLOY_AI"].required is False


def test_litellm_ledger_resource_is_owned() -> None:
    updated = build_shared_core_ledger(
        existing_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="nextcloud_service",
                    resource_id="nextcloud-1",
                    scope="stack:wizard-stack:nextcloud-service",
                ),
            ),
        ),
        stack_name="wizard-stack",
        network_resource_id="network-1",
        postgres_resource_id="postgres-1",
        redis_resource_id=None,
        mail_relay_resource_id=None,
        litellm_resource_id="litellm-1",
    )

    assert ("shared_core_litellm", "litellm-1", "stack:wizard-stack:shared-litellm") in {
        (resource.resource_type, resource.resource_id, resource.scope)
        for resource in updated.resources
    }
    assert ("nextcloud_service", "nextcloud-1", "stack:wizard-stack:nextcloud-service") in {
        (resource.resource_type, resource.resource_id, resource.scope)
        for resource in updated.resources
    }


def test_shared_core_matching_healthy_hash_skips_update_and_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("nextcloud",))
    litellm_env = {
        "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
        "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
    }
    generated_keys = _generated_keys()
    rendered_compose = _render_compose_file(plan, {}, litellm_env, generated_keys)
    _write_hash_checkpoint(
        tmp_path,
        service_key=plan.network_name,
        rendered_compose=rendered_compose,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=plan.network_name,
        compose_id="cmp-shared",
        project_name="wizard-stack",
        compose_file=rendered_compose.compose_file,
    )
    readiness_api = _FakeLiteLLMAdminApi({"status": "connected", "db": "connected"})
    container_lookups: list[str] = []
    allocation_checks: list[SharedPostgresAllocation] = []
    redis_checks: list[str] = []

    def fake_find_container_name(service_name: str) -> str | None:
        container_lookups.append(service_name)
        if plan.postgres is not None and service_name == plan.postgres.service_name:
            return "postgres-ctr"
        if plan.redis is not None and service_name == plan.redis.service_name:
            return "redis-ctr"
        return None

    def fake_can_connect_as_allocation(
        container_name: str, allocation: SharedPostgresAllocation
    ) -> bool:
        assert container_name == "postgres-ctr"
        allocation_checks.append(allocation)
        return True

    def fake_redis_is_ready(container_name: str) -> bool:
        redis_checks.append(container_name)
        return container_name == "redis-ctr"

    monkeypatch.setattr(shared_core_module, "_find_container_name", fake_find_container_name)
    monkeypatch.setattr(
        shared_core_module,
        "_can_connect_as_allocation",
        fake_can_connect_as_allocation,
    )
    monkeypatch.setattr(shared_core_module, "_redis_is_ready", fake_redis_is_ready)

    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        litellm_env=litellm_env,
        client=client,
        litellm_generated_keys=generated_keys,
        litellm_admin_api=readiness_api,
        state_dir=tmp_path,
    )

    backend.create_network(plan.network_name)

    client.assert_unchanged_service(plan.network_name)
    assert readiness_api.readiness_calls == 1
    assert plan.postgres is not None
    assert plan.redis is not None
    assert plan.litellm is not None
    assert allocation_checks == [plan.allocations[0].postgres, plan.litellm.postgres]
    assert redis_checks == ["redis-ctr"]
    assert container_lookups.count(plan.postgres.service_name) == 1
    assert container_lookups.count(plan.redis.service_name) == 1


def test_shared_core_unhealthy_litellm_blocks_noop_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("nextcloud",))
    litellm_env = {
        "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
        "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
    }
    generated_keys = _generated_keys()
    rendered_compose = _render_compose_file(plan, {}, litellm_env, generated_keys)
    _write_hash_checkpoint(
        tmp_path,
        service_key=plan.network_name,
        rendered_compose=rendered_compose.compose_file,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=plan.network_name,
        compose_id="cmp-shared",
        project_name="wizard-stack",
        compose_file=rendered_compose.compose_file,
    )

    monkeypatch.setattr(
        shared_core_module,
        "_find_container_name",
        lambda service_name: "postgres-ctr"
        if plan.postgres is not None and service_name == plan.postgres.service_name
        else "redis-ctr"
        if plan.redis is not None and service_name == plan.redis.service_name
        else None,
    )
    monkeypatch.setattr(
        shared_core_module,
        "_can_connect_as_allocation",
        lambda container_name, allocation: True,
    )
    monkeypatch.setattr(shared_core_module, "_redis_is_ready", lambda container_name: True)

    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        litellm_env=litellm_env,
        client=client,
        litellm_generated_keys=generated_keys,
        litellm_admin_api=_FakeLiteLLMAdminApi({"status": "starting", "db": "connected"}),
        state_dir=tmp_path,
    )

    backend.create_network(plan.network_name)

    client.assert_single_update_deploy_pair(plan.network_name)


def test_shared_core_validates_litellm_virtual_key_state_matches_admin_db() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("coder",))
    generated_keys = _generated_keys()
    matching_records = tuple(
        LiteLLMVirtualKeyRecord(
            key=value,
            key_alias=consumer,
            team_id=f"team-{consumer}",
            models=("local-model.internal/unsloth-active",)
            if consumer == "coder-hermes"
            else (),
            metadata={"consumer": consumer, "managed_by": "dokploy-wizard"},
        )
        for consumer, value in generated_keys.virtual_keys.items()
    )
    matching_backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        litellm_generated_keys=generated_keys,
        litellm_consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",)
        },
        litellm_admin_api=_FakeLiteLLMAdminApi(
            {"status": "connected", "db": "connected"},
            keys=matching_records,
        ),
    )
    drifted_records = tuple(
        LiteLLMVirtualKeyRecord(
            key="sk-coder-hermes-db-accepted-key"
            if record.key_alias == "coder-hermes"
            else record.key,
            key_alias=record.key_alias,
            team_id=record.team_id,
            models=record.models,
            metadata=record.metadata,
        )
        for record in matching_records
    )
    drifted_backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        litellm_generated_keys=generated_keys,
        litellm_consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",)
        },
        litellm_admin_api=_FakeLiteLLMAdminApi(
            {"status": "connected", "db": "connected"},
            keys=drifted_records,
        ),
    )

    assert matching_backend.validate_litellm_virtual_keys() is True
    assert drifted_backend.validate_litellm_virtual_keys() is False

    # The sync sidecar appends explicit sync/* aliases to consumer keys for
    # /v1/models visibility; such keys are still valid (wizard list is a floor).
    sidecar_expanded_records = tuple(
        LiteLLMVirtualKeyRecord(
            key=record.key,
            key_alias=record.key_alias,
            team_id=record.team_id,
            models=(
                (*record.models, "sync/openrouter/deepseek/deepseek-v4.1-flash")
                if record.key_alias == "coder-hermes"
                else record.models
            ),
            metadata=record.metadata,
        )
        for record in matching_records
    )
    sidecar_expanded_backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        litellm_generated_keys=generated_keys,
        litellm_consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",)
        },
        litellm_admin_api=_FakeLiteLLMAdminApi(
            {"status": "connected", "db": "connected"},
            keys=sidecar_expanded_records,
        ),
    )
    assert sidecar_expanded_backend.validate_litellm_virtual_keys() is True


def test_shared_core_find_container_name_prefers_exact_manual_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: list[str], check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                "openmerge-shared-postgres\n"
                "openmerge-shared-a1b2c3-openmerge-shared-postgres-1\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("dokploy_wizard.dokploy.shared_core.subprocess.run", fake_run)

    assert shared_core_module._find_container_name("openmerge-shared-postgres") == (
        "openmerge-shared-postgres"
    )


def test_shared_core_runtime_ready_uses_resolved_dokploy_container_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_shared_core_plan(stack_name="openmerge", enabled_packs=("nextcloud",))
    readiness_api = _FakeLiteLLMAdminApi({"status": "connected", "db": "connected"})
    postgres_container_names: list[str] = []
    redis_container_names: list[str] = []

    def fake_run(
        command: list[str], check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        assert command[:2] == ["docker", "ps"]
        service_filter = command[3]
        if service_filter == "label=com.docker.compose.service=openmerge-shared-postgres":
            stdout = "openmerge-shared-yqjzwd-openmerge-shared-postgres-1\n"
        elif service_filter == "label=com.docker.compose.service=openmerge-shared-redis":
            stdout = "openmerge-shared-yqjzwd-openmerge-shared-redis-1\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    def fake_can_connect_as_allocation(
        container_name: str, allocation: SharedPostgresAllocation
    ) -> bool:
        del allocation
        postgres_container_names.append(container_name)
        return True

    def fake_redis_is_ready(container_name: str) -> bool:
        redis_container_names.append(container_name)
        return True

    monkeypatch.setattr("dokploy_wizard.dokploy.shared_core.subprocess.run", fake_run)
    monkeypatch.setattr(
        shared_core_module,
        "_can_connect_as_allocation",
        fake_can_connect_as_allocation,
    )
    monkeypatch.setattr(shared_core_module, "_redis_is_ready", fake_redis_is_ready)

    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="openmerge",
        plan=plan,
        litellm_generated_keys=_generated_keys(),
        litellm_admin_api=readiness_api,
    )

    assert backend._shared_core_runtime_ready_for_noop() is True
    assert postgres_container_names == [
        "openmerge-shared-yqjzwd-openmerge-shared-postgres-1",
        "openmerge-shared-yqjzwd-openmerge-shared-postgres-1",
    ]
    assert redis_container_names == ["openmerge-shared-yqjzwd-openmerge-shared-redis-1"]


def test_litellm_allowlists_keep_local_alias_for_every_consumer() -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder",),
    )

    allowlists = build_litellm_consumer_model_allowlists(
        flat_env={
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
        },
        plan=plan,
    )

    assert allowlists == {
        "coder-hermes": ("local-model.internal/unsloth-active",),
        "coder-workspace-issuer": ("*",),
        "dokploy-ai": ("local-model.internal/unsloth-active",),
        "nextcloud": ("local-model.internal/unsloth-active",),
    }


class _FakeLiteLLMAdminApi:
    def __init__(
        self,
        readiness_payload: dict[str, object],
        *,
        teams: tuple[LiteLLMTeamRecord, ...] = (),
        keys: tuple[LiteLLMVirtualKeyRecord, ...] = (),
    ) -> None:
        self._readiness_payload = readiness_payload
        self._teams = {team.team_alias: team for team in teams}
        self._keys = {key.key_alias: key for key in keys}
        self.readiness_calls = 0

    def readiness(self) -> dict[str, object]:
        self.readiness_calls += 1
        return dict(self._readiness_payload)

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return tuple(self._teams.values())

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(
            team_id=f"team-{team_alias}",
            team_alias=team_alias,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._teams[team_alias] = team
        return team

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(
            team_id=team_id,
            team_alias=team_alias,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._teams[team_alias] = team
        return team

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return tuple(self._keys.values())

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._keys[key_alias] = record
        return record

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._keys[key_alias] = record
        return record

    def delete_key(self, *, key_alias: str) -> None:
        self._keys.pop(key_alias, None)

    def delete_team(self, *, team_id: str) -> None:
        for alias, team in list(self._teams.items()):
            if team.team_id == team_id:
                del self._teams[alias]


def _generated_keys() -> LiteLLMGeneratedKeys:
    return LiteLLMGeneratedKeys(
        format_version=STATE_FORMAT_VERSION,
        master_key="sk-master-fake-test-key",
        salt_key="sk-salt-fake-test-key",
        virtual_keys={
            "coder-hermes": "sk-hermes-fake-test-key",
            "coder-kdense": "sk-kdense-fake-test-key",
            "dokploy-ai": "sk-dokploy-ai-fake-test-key",
            "nextcloud": "sk-nextcloud-fake-test-key",
        },
    )


def _embedded_litellm_model_block(compose: str, model_name: str) -> str:
    start = f'        - model_name: "{model_name}"\n'
    start_index = compose.index(start) + len(start)
    remaining = compose[start_index:]
    marker_indexes = tuple(
        index
        for marker in ("        - model_name:", "      litellm_settings:")
        if (index := remaining.find(marker)) != -1
    )
    end_index = min(marker_indexes) if marker_indexes else len(remaining)
    return remaining[:end_index]




def _provider_payload_uses_dokploy_ai_virtual_key(
    payload: Mapping[str, object], generated_keys: LiteLLMGeneratedKeys
) -> bool:
    return (
        payload.get("apiKey") != generated_keys.master_key
        and payload.get("apiKey") == generated_keys.virtual_keys["dokploy-ai"]
    )


def _require_provider_payload_uses_dokploy_ai_virtual_key(is_valid: bool) -> None:
    if not is_valid:
        raise AssertionError(
            "Dokploy AI provider must use the dedicated dokploy-ai virtual key, not a master or drifted key."
        )


def _require_mutation_count(actual: int, expected: int, *, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"Expected {expected} {label} mutation(s), got {actual}.")


def _managed_litellm_records_for(
    generated_keys: LiteLLMGeneratedKeys,
    *,
    consumer_model_allowlists: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[tuple[LiteLLMTeamRecord, ...], tuple[LiteLLMVirtualKeyRecord, ...]]:
    allowlists = consumer_model_allowlists or {}
    teams: list[LiteLLMTeamRecord] = []
    keys: list[LiteLLMVirtualKeyRecord] = []
    for consumer, key in generated_keys.virtual_keys.items():
        models = tuple(dict.fromkeys(allowlists.get(consumer, ())))
        metadata = {"consumer": consumer, "managed_by": "dokploy-wizard"}
        teams.append(
            LiteLLMTeamRecord(
                team_id=f"team-{consumer}",
                team_alias=consumer,
                models=models,
                metadata=metadata,
            )
        )
        keys.append(
            LiteLLMVirtualKeyRecord(
                key=key,
                key_alias=consumer,
                team_id=f"team-{consumer}",
                models=models,
                metadata=metadata,
            )
        )
    return tuple(teams), tuple(keys)


def _write_hash_checkpoint(state_dir: Path, *, service_key: str, rendered_compose: object) -> None:
    compose_file = getattr(rendered_compose, "compose_file", rendered_compose)
    env_specs = getattr(rendered_compose, "env_specs", ())
    assert isinstance(compose_file, str)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("preflight",),
            compose_artifact_hashes={
                service_key: ComposeArtifactHashState.from_rendered_compose(
                    service_id=service_key,
                    rendered_compose=compose_file,
                    env_specs=env_specs,
                )
            },
        ),
    )


def test_ai_provider_create_records_payload_on_fake() -> None:
    from tests.unit.fake_dokploy import FakeDokployApiClient

    fake = FakeDokployApiClient()
    fake.ai_provider_create(
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-provider-fake-test-key",
        model="local-model.internal/unsloth-active",
        is_enabled=True,
    )

    assert len(fake._ai_provider_creates) == 1
    create = fake._ai_provider_creates[0]
    assert create["name"] == "Dokploy Wizard LiteLLM"
    assert create["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    assert create["apiKey"] == "sk-provider-fake-test-key"
    assert create["model"] == "local-model.internal/unsloth-active"
    assert create["isEnabled"] is True


def test_ai_provider_update_records_payload_on_fake() -> None:
    from tests.unit.fake_dokploy import FakeDokployApiClient

    fake = FakeDokployApiClient()
    fake.ai_provider_update(
        ai_id="ai-wizard-1",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="local-model.internal/unsloth-active",
        is_enabled=True,
    )

    assert len(fake._ai_provider_updates) == 1
    update = fake._ai_provider_updates[0]
    assert update["aiId"] == "ai-wizard-1"
    assert update["model"] == "local-model.internal/unsloth-active"


def test_ai_client_request_payloads_via_fake_request_fn() -> None:
    """Verify actual API endpoint paths and JSON bodies via fake request_fn."""
    from dokploy_wizard.dokploy.client import DokployApiClient

    calls: list[tuple[str, str, object | None]] = []

    def fake_request(req: object) -> object:
        import json
        from urllib import parse, request

        assert isinstance(req, request.Request)
        method = req.get_method()
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        parsed = parse.urlparse(url)
        path = parsed.path
        raw_data = req.data
        body: object | None = None
        if method == "POST" and raw_data is not None:
            if isinstance(raw_data, bytes):
                body = json.loads(raw_data.decode("utf-8"))
            else:
                body = json.loads(str(raw_data))
        calls.append((method, path, body))
        if path == "/api/ai.delete":
            return True
        if method == "POST":
            return {
                "data": {
                    "aiId": "ai-fake-1",
                    "name": "Dokploy Wizard LiteLLM",
                    "apiUrl": "http://openmerge-shared-litellm:4000/v1",
                    "apiKey": "sk-master-fake-test-key",
                    "model": "local-model.internal/unsloth-active",
                    "isEnabled": True,
                }
            }
        return []

    client = DokployApiClient(
        api_url="http://dokploy.test/api",
        api_key="fake-key",
        request_fn=fake_request,
    )

    client.ai_providers_all()
    assert len(calls) == 1
    assert calls[0] == ("GET", "/api/ai.getAll", None)

    client.ai_provider_create(
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="local-model.internal/unsloth-active",
        is_enabled=True,
    )
    assert len(calls) == 2
    assert calls[1] == (
        "POST",
        "/api/ai.create",
        {
            "name": "Dokploy Wizard LiteLLM",
            "apiUrl": "http://openmerge-shared-litellm:4000/v1",
            "apiKey": "sk-master-fake-test-key",
            "model": "local-model.internal/unsloth-active",
            "isEnabled": True,
        },
    )

    client.ai_provider_update(
        ai_id="ai-fake-1",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="local-model.internal/unsloth-active",
        is_enabled=True,
    )
    assert len(calls) == 3
    assert calls[2] == (
        "POST",
        "/api/ai.update",
        {
            "aiId": "ai-fake-1",
            "name": "Dokploy Wizard LiteLLM",
            "apiUrl": "http://openmerge-shared-litellm:4000/v1",
            "apiKey": "sk-master-fake-test-key",
            "model": "local-model.internal/unsloth-active",
            "isEnabled": True,
        },
    )

    client.ai_provider_delete(ai_id="ai-fake-1")
    assert len(calls) == 4
    assert calls[3] == ("POST", "/api/ai.delete", {"aiId": "ai-fake-1"})


def test_ai_get_all_tolerates_hidden_api_key() -> None:
    """Current upstream ai.getAll selects the row without apiKey."""
    from urllib import request

    from dokploy_wizard.dokploy.client import DokployApiClient

    row_without_key = {
        "aiId": "ai-fake-1",
        "name": "Dokploy Wizard LiteLLM",
        "apiUrl": "http://openmerge-shared-litellm:4000/v1",
        "model": "local-model.internal/unsloth-active",
        "isEnabled": True,
    }

    def fake_request(req: object) -> object:
        assert isinstance(req, request.Request)
        return [row_without_key]

    client = DokployApiClient(
        api_url="http://dokploy.test/api",
        api_key="fake-key",
        request_fn=fake_request,
    )

    providers = client.ai_providers_all()

    assert providers[0].api_key == ""
    assert providers[0].ai_id == "ai-fake-1"


def test_ai_model_alias_default_with_no_env() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    assert _dokploy_ai_model_alias({}) == "opencode-go/deepseek-v4.1-flash"


def test_ai_model_alias_custom_provider_with_dot_without_local_base_url_falls_back() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "custom.example.internal",
        "AI_DEFAULT_MODEL": "agent-model",
    })
    assert result == "opencode-go/deepseek-v4.1-flash"


def test_ai_model_alias_opencode_go_provider_uses_configured_alias() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "opencode-go",
        "AI_DEFAULT_MODEL": "deepseek-v4.1-flash",
    })
    assert result == "opencode-go/deepseek-v4.1-flash"


def test_ai_model_alias_openrouter_provider_uses_configured_alias() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "openrouter",
        "AI_DEFAULT_MODEL": "openrouter/hunter-alpha",
    })
    assert result == "openrouter/openrouter/hunter-alpha"


def test_ai_model_alias_local_provider_maps_to_default_local() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "local",
        "AI_DEFAULT_MODEL": "custom-model",
        "LITELLM_LOCAL_BASE_URL": "http://local-litellm-upstream.example/v1",
        "LITELLM_LOCAL_MODEL": "custom-model",
        "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
    })
    assert result.startswith("local-model.internal/")
    assert result == "local-model.internal/custom-model"


def test_ai_model_alias_partial_env_falls_back_to_default() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    assert _dokploy_ai_model_alias({"AI_DEFAULT_PROVIDER": "local"}) == "opencode-go/deepseek-v4.1-flash"
    assert _dokploy_ai_model_alias({"AI_DEFAULT_MODEL": "model-x"}) == "opencode-go/deepseek-v4.1-flash"


_DEFAULT_AI_ALIAS = "opencode-go/deepseek-v4.1-flash"


def _ai_reconcile(
    fake: FakeDokployApiClient,
    generated: LiteLLMGeneratedKeys,
    *,
    litellm_service_name: str = "openmerge-shared-litellm",
    litellm_env: dict[str, str] | None = None,
    dokploy_ai_allowlist: tuple[str, ...] = (_DEFAULT_AI_ALIAS,),
) -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name=litellm_service_name,
        generated_keys=generated,
        litellm_env=litellm_env,
        consumer_allowlists={"dokploy-ai": dokploy_ai_allowlist},
    )


def _wizard_provider_row(
    *,
    ai_id: str,
    model: str,
    name: str | None = None,
    api_url: str = "http://openmerge-shared-litellm:4000/v1",
    api_key: str = "sk-dokploy-ai-fake-test-key",
    is_enabled: bool = True,
) -> DokployAiProvider:
    if name is None or name == _DEFAULT_AI_ALIAS:
        row_name = "Dokploy Wizard LiteLLM"
    else:
        row_name = f"Dokploy Wizard LiteLLM · {name}"
    return DokployAiProvider(
        ai_id=ai_id,
        name=row_name,
        api_url=api_url,
        api_key=api_key,
        model=model,
        is_enabled=is_enabled,
    )


def test_ai_reconcile_creates_provider_when_missing() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated)

    assert len(fake._ai_provider_creates) == 1
    assert len(fake._ai_provider_updates) == 0
    assert fake._ai_provider_creates[0]["name"] == "Dokploy Wizard LiteLLM"
    assert fake._ai_provider_creates[0]["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    _require_provider_payload_uses_dokploy_ai_virtual_key(
        _provider_payload_uses_dokploy_ai_virtual_key(fake._ai_provider_creates[0], generated)
    )
    assert fake._ai_provider_creates[0]["model"] == _DEFAULT_AI_ALIAS
    assert fake._ai_provider_creates[0]["isEnabled"] is True


def test_ai_reconcile_updates_provider_when_present() -> None:
    fake = FakeDokployApiClient()
    existing = _wizard_provider_row(
        ai_id="ai-wizard-existing",
        model="bare-model",
        api_url="http://old-url:4000/v1",
        api_key="<redacted-old-provider-key>",
    )
    fake._ai_providers = [existing]
    generated = _generated_keys()

    _ai_reconcile(fake, generated)

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 1
    update = fake._ai_provider_updates[0]
    assert update["aiId"] == "ai-wizard-existing"
    _require_provider_payload_uses_dokploy_ai_virtual_key(
        _provider_payload_uses_dokploy_ai_virtual_key(update, generated)
    )
    assert update["model"] == _DEFAULT_AI_ALIAS


def test_ai_reconcile_does_not_create_duplicate_on_repeat() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated)
    _ai_reconcile(fake, generated)
    _ai_reconcile(fake, generated)

    assert len(fake._ai_provider_creates) == 1
    _require_mutation_count(len(fake._ai_provider_updates), 0, label="provider update")
    _require_mutation_count(len(fake._ai_provider_deletes), 0, label="provider delete")


def test_ai_reconcile_uses_canonical_model_alias_not_bare() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated)

    for create in fake._ai_provider_creates:
        assert create["model"] != "unsloth-active"
        assert create["model"] == _DEFAULT_AI_ALIAS
    for update in fake._ai_provider_updates:
        assert update["model"] != "unsloth-active"
        assert update["model"] == _DEFAULT_AI_ALIAS


def test_ai_reconcile_skips_when_no_generated_keys() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=None,
        consumer_allowlists={},
    )

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 0


def test_ai_reconcile_fails_clearly_when_dokploy_ai_virtual_key_missing() -> None:
    from dokploy_wizard.core import SharedCoreError

    fake = FakeDokployApiClient()
    generated = _generated_keys()
    missing_dokploy_ai = LiteLLMGeneratedKeys(
        format_version=generated.format_version,
        master_key=generated.master_key,
        salt_key=generated.salt_key,
        virtual_keys={
            consumer: key
            for consumer, key in generated.virtual_keys.items()
            if consumer != "dokploy-ai"
        },
    )

    with pytest.raises(SharedCoreError, match="dokploy-ai"):
        _ai_reconcile(fake, missing_dokploy_ai)

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 0


def test_ai_reconcile_uses_internal_litellm_url() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated, litellm_env={})

    assert fake._ai_provider_creates[0]["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    api_url = fake._ai_provider_creates[0]["apiUrl"]
    assert isinstance(api_url, str)
    assert "api.openai.com" not in api_url


def test_ai_reconcile_uses_custom_provider_alias() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(
        fake,
        generated,
        litellm_service_name="my-stack-shared-litellm",
        litellm_env={
            "AI_DEFAULT_PROVIDER": "custom.example.internal",
            "AI_DEFAULT_MODEL": "agent-model",
        },
    )

    assert len(fake._ai_provider_creates) == 1
    assert fake._ai_provider_creates[0]["model"] == _DEFAULT_AI_ALIAS
    assert fake._ai_provider_creates[0]["apiUrl"] == "http://my-stack-shared-litellm:4000/v1"


def test_ai_reconcile_preserves_user_owned_providers_and_creates_wizard_provider() -> None:
    fake = FakeDokployApiClient()
    user_provider = DokployAiProvider(
        ai_id="ai-user-provider",
        name="Personal LiteLLM",
        api_url="https://user-provider.example/v1",
        api_key="<redacted-user-provider-key>",
        model="user/model",
        is_enabled=False,
    )
    fake._ai_providers = [user_provider]
    generated = _generated_keys()

    _ai_reconcile(fake, generated)

    assert fake.ai_providers_all()[0] == user_provider
    assert len(fake._ai_provider_creates) == 1
    assert len(fake._ai_provider_updates) == 0
    _require_provider_payload_uses_dokploy_ai_virtual_key(
        _provider_payload_uses_dokploy_ai_virtual_key(fake._ai_provider_creates[0], generated)
    )


def test_ai_reconcile_noops_when_existing_wizard_provider_matches() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()
    fake._ai_providers = [
        _wizard_provider_row(ai_id="ai-wizard-existing", model=_DEFAULT_AI_ALIAS)
    ]

    _ai_reconcile(fake, generated)

    _require_mutation_count(len(fake._ai_provider_creates), 0, label="provider create")
    _require_mutation_count(len(fake._ai_provider_updates), 0, label="provider update")
    _require_mutation_count(len(fake._ai_provider_deletes), 0, label="provider delete")


def test_ai_reconcile_treats_hidden_api_key_as_not_drifted() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()
    fake._ai_providers = [
        _wizard_provider_row(ai_id="ai-wizard-existing", model=_DEFAULT_AI_ALIAS, api_key="")
    ]

    _ai_reconcile(fake, generated)

    # Current upstream ai.getAll hides apiKey; a blank visible key must not
    # force an update on every pass or the AI rows would never be no-op.
    _require_mutation_count(len(fake._ai_provider_updates), 0, label="provider update")


def test_ai_reconcile_updates_disabled_or_drifted_wizard_provider() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()
    fake._ai_providers = [
        _wizard_provider_row(
            ai_id="ai-wizard-existing",
            model="wrong/model",
            api_url="http://wrong-litellm:4000/v1",
            api_key="<redacted-wrong-provider-key>",
            is_enabled=False,
        )
    ]

    _ai_reconcile(fake, generated)

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 1
    update = fake._ai_provider_updates[0]
    assert update["aiId"] == "ai-wizard-existing"
    assert update["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    assert update["model"] == _DEFAULT_AI_ALIAS
    assert update["isEnabled"] is True
    _require_provider_payload_uses_dokploy_ai_virtual_key(
        _provider_payload_uses_dokploy_ai_virtual_key(update, generated)
    )


def test_ai_reconcile_seeds_one_row_per_allowed_alias() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(
        fake,
        generated,
        dokploy_ai_allowlist=(
            _DEFAULT_AI_ALIAS,
            "openrouter/openrouter/hunter-alpha",
            "local-model.internal/unsloth-active",
        ),
    )

    assert len(fake._ai_provider_creates) == 3
    by_model = {str(create["model"]): create for create in fake._ai_provider_creates}
    assert set(by_model) == {
        _DEFAULT_AI_ALIAS,
        "openrouter/openrouter/hunter-alpha",
        "local-model.internal/unsloth-active",
    }
    assert by_model[_DEFAULT_AI_ALIAS]["name"] == "Dokploy Wizard LiteLLM"
    assert (
        by_model["openrouter/openrouter/hunter-alpha"]["name"]
        == "Dokploy Wizard LiteLLM · openrouter/openrouter/hunter-alpha"
    )
    for create in fake._ai_provider_creates:
        _require_provider_payload_uses_dokploy_ai_virtual_key(
            _provider_payload_uses_dokploy_ai_virtual_key(create, generated)
        )
        assert create["isEnabled"] is True


def test_ai_reconcile_deletes_wizard_rows_outside_allowed_aliases() -> None:
    stale = _wizard_provider_row(
        ai_id="ai-wizard-stale",
        name="retired/alias",
        model="retired/alias",
    )
    user_keeper = DokployAiProvider(
        ai_id="ai-user-keeper",
        name="Personal LiteLLM",
        api_url="https://user-provider.example/v1",
        api_key="<redacted-user-provider-key>",
        model="user/model",
        is_enabled=True,
    )
    fake = FakeDokployApiClient()
    fake._ai_providers = [
        _wizard_provider_row(ai_id="ai-wizard-default", model=_DEFAULT_AI_ALIAS),
        stale,
        user_keeper,
    ]
    generated = _generated_keys()

    _ai_reconcile(fake, generated)

    _require_mutation_count(len(fake._ai_provider_creates), 0, label="provider create")
    _require_mutation_count(len(fake._ai_provider_updates), 0, label="provider update")
    assert fake._ai_provider_deletes == ["ai-wizard-stale"]
    assert fake.ai_providers_all()[1] == user_keeper


def test_ai_reconcile_deduplicates_alias_rows_keeping_one() -> None:
    alias = "openrouter/openrouter/hunter-alpha"
    keeper = _wizard_provider_row(ai_id="ai-wizard-keeper", name=alias, model=alias)
    duplicate = _wizard_provider_row(ai_id="ai-wizard-duplicate", name=alias, model=alias)
    fake = FakeDokployApiClient()
    fake._ai_providers = [
        _wizard_provider_row(ai_id="ai-wizard-default", model=_DEFAULT_AI_ALIAS),
        keeper,
        duplicate,
    ]
    generated = _generated_keys()

    _ai_reconcile(fake, generated, dokploy_ai_allowlist=(_DEFAULT_AI_ALIAS, alias))

    _require_mutation_count(len(fake._ai_provider_creates), 0, label="provider create")
    _require_mutation_count(len(fake._ai_provider_updates), 0, label="provider update")
    assert fake._ai_provider_deletes == ["ai-wizard-duplicate"]


def test_ai_reconcile_wildcard_allowlist_widens_with_live_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_core_module,
        "_litellm_exposed_aliases",
        lambda **kwargs: ("sync/sglang/qwen3-coder", _DEFAULT_AI_ALIAS),
    )
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated, dokploy_ai_allowlist=(_DEFAULT_AI_ALIAS, "sync/*"))

    models = {str(create["model"]) for create in fake._ai_provider_creates}
    assert models == {_DEFAULT_AI_ALIAS, "sync/sglang/qwen3-coder"}
    # The sync wildcard itself is a key-level grant, never a provider row.
    assert "sync/*" not in models


def test_ai_reconcile_wildcard_allowlist_survives_unreachable_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_core_module,
        "_litellm_exposed_aliases",
        lambda **kwargs: None,
    )
    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ai_reconcile(fake, generated, dokploy_ai_allowlist=(_DEFAULT_AI_ALIAS, "sync/*"))

    models = {str(create["model"]) for create in fake._ai_provider_creates}
    assert models == {_DEFAULT_AI_ALIAS}


def test_ai_reconcile_renames_legacy_base_row_to_default_alias_row() -> None:
    fake = FakeDokployApiClient()
    generated = _generated_keys()
    legacy = _wizard_provider_row(
        ai_id="ai-wizard-legacy",
        model="opencode-go/legacy-model",
        api_key="<redacted-legacy-key>",
    )
    fake._ai_providers = [legacy]

    _ai_reconcile(fake, generated)

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 1
    assert fake._ai_provider_updates[0]["aiId"] == "ai-wizard-legacy"
    assert fake._ai_provider_updates[0]["model"] == _DEFAULT_AI_ALIAS
    assert fake._ai_provider_updates[0]["name"] == "Dokploy Wizard LiteLLM"


def test_litellm_runtime_reconciles_dokploy_ai_provider_even_when_virtual_keys_unchanged() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("coder",))
    generated = _generated_keys()
    allowlists: dict[str, tuple[str, ...]] = {
        "coder-hermes": ("local-model.internal/unsloth-active",),
    }
    teams, keys = _managed_litellm_records_for(
        generated,
        consumer_model_allowlists=allowlists,
    )
    dokploy_client = FakeDokployApiClient()
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        client=dokploy_client,
        litellm_generated_keys=generated,
        litellm_consumer_model_allowlists=allowlists,
        litellm_admin_api=_FakeLiteLLMAdminApi(
            {"status": "connected", "db": "connected"},
            teams=teams,
            keys=keys,
        ),
    )

    backend.reconcile_litellm_runtime()

    _require_mutation_count(len(dokploy_client._ai_provider_creates), 1, label="provider create")
    _require_mutation_count(len(dokploy_client._ai_provider_updates), 0, label="provider update")
    _require_provider_payload_uses_dokploy_ai_virtual_key(
        _provider_payload_uses_dokploy_ai_virtual_key(
            dokploy_client._ai_provider_creates[0],
            generated,
        )
    )


def test_litellm_runtime_surfaces_dokploy_ai_provider_seed_failures() -> None:
    from dokploy_wizard.core import SharedCoreError
    from dokploy_wizard.dokploy.client import DokployApiError

    class FailingAiProviderClient(FakeDokployApiClient):
        def ai_providers_all(self):  # type: ignore[no-untyped-def]
            raise DokployApiError("Dokploy AI provider list failed visibly.")

    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("coder",))
    generated = _generated_keys()
    allowlists: dict[str, tuple[str, ...]] = {
        "coder-hermes": ("local-model.internal/unsloth-active",),
    }
    teams, keys = _managed_litellm_records_for(
        generated,
        consumer_model_allowlists=allowlists,
    )
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="token",
        stack_name="wizard-stack",
        plan=plan,
        client=FailingAiProviderClient(),
        litellm_generated_keys=generated,
        litellm_consumer_model_allowlists=allowlists,
        litellm_admin_api=_FakeLiteLLMAdminApi(
            {"status": "connected", "db": "connected"},
            teams=teams,
            keys=keys,
        ),
    )

    with pytest.raises(SharedCoreError, match="Dokploy AI provider list failed visibly"):
        backend.reconcile_litellm_runtime()


def test_verify_dokploy_ai_provider_reports_only_redacted_key_metadata() -> None:
    from dokploy_wizard.dokploy.client import DokployAiProvider
    from dokploy_wizard.dokploy.shared_core import verify_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()
    raw_key = generated.virtual_keys["dokploy-ai"]
    fake._ai_providers = [
        DokployAiProvider(
            ai_id="ai-wizard-existing",
            name="Dokploy Wizard LiteLLM",
            api_url="http://openmerge-shared-litellm:4000/v1",
            api_key=raw_key,
            model="opencode-go/deepseek-v4.1-flash",
            is_enabled=True,
        )
    ]

    result = verify_dokploy_ai_provider(
        fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )
    payload = result.to_dict()
    rendered = str(payload)

    assert result.passed is True
    assert raw_key not in rendered
    assert generated.master_key not in rendered
    assert "matches_dokploy_ai_virtual_key" in result.detail
    assert "sha256:" in result.detail
    assert "api_key" not in result.detail


def test_verify_dokploy_ai_provider_fails_when_provider_uses_master_key_without_leaking_it() -> None:
    from dokploy_wizard.dokploy.client import DokployAiProvider
    from dokploy_wizard.dokploy.shared_core import verify_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()
    fake._ai_providers = [
        DokployAiProvider(
            ai_id="ai-wizard-existing",
            name="Dokploy Wizard LiteLLM",
            api_url="http://openmerge-shared-litellm:4000/v1",
            api_key=generated.master_key,
            model="opencode-go/deepseek-v4.1-flash",
            is_enabled=True,
        )
    ]

    result = verify_dokploy_ai_provider(
        fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )
    rendered = str(result.to_dict())

    assert result.passed is False
    assert "uses_litellm_master_key" in result.detail
    assert generated.master_key not in rendered
    assert generated.virtual_keys["dokploy-ai"] not in rendered


def test_verify_dokploy_ai_provider_fails_on_duplicate_wizard_provider() -> None:
    from dokploy_wizard.dokploy.client import DokployAiProvider
    from dokploy_wizard.dokploy.shared_core import verify_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()
    provider = DokployAiProvider(
        ai_id="ai-wizard-existing",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key=generated.virtual_keys["dokploy-ai"],
        model="opencode-go/deepseek-v4.1-flash",
        is_enabled=True,
    )
    fake._ai_providers = [
        provider,
        DokployAiProvider(
            ai_id="ai-wizard-duplicate",
            name=provider.name,
            api_url=provider.api_url,
            api_key=provider.api_key,
            model=provider.model,
            is_enabled=provider.is_enabled,
        ),
    ]

    result = verify_dokploy_ai_provider(
        fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )

    assert result.passed is False
    assert "provider_state_drift" in result.detail
    assert "duplicate_aliases" in result.detail


def test_rendered_litellm_service_attaches_to_dokploy_network_with_alias() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(plan, {}, {}, _generated_keys())
    compose = rendered.compose_file

    assert "      shared:\n        aliases:\n          - wizard-stack-shared-litellm" in compose
    assert "      dokploy-network:\n        aliases:\n          - wizard-stack-shared-litellm" in compose
    assert "  dokploy-network:\n    external: true" in compose
    assert '      - "127.0.0.1:4000:4000"' in compose
    assert _generated_keys().virtual_keys["dokploy-ai"] not in compose


def test_verify_dokploy_ai_provider_runs_test_connection_when_available() -> None:
    from dokploy_wizard.dokploy.client import (
        DokployAiProvider,
        DokployAiProviderTestConnectionResult,
    )
    from dokploy_wizard.dokploy.shared_core import verify_dokploy_ai_provider

    class TestConnectionClient(FakeDokployApiClient):
        calls: list[tuple[str, str, str]]

        def __init__(self) -> None:
            super().__init__()
            self.calls = []

        def ai_provider_test_connection(self, *, api_url: str, api_key: str, model: str):  # type: ignore[no-untyped-def]
            self.calls.append((api_url, api_key, model))
            return DokployAiProviderTestConnectionResult(success=True, message="ok")

    generated = _generated_keys()
    client = TestConnectionClient()
    client._ai_providers = [
        DokployAiProvider(
            ai_id="ai-wizard-existing",
            name="Dokploy Wizard LiteLLM",
            api_url="http://openmerge-shared-litellm:4000/v1",
            api_key=generated.virtual_keys["dokploy-ai"],
            model="opencode-go/deepseek-v4.1-flash",
            is_enabled=True,
        )
    ]

    result = verify_dokploy_ai_provider(
        client,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )

    assert result.passed is True
    assert "test_connection_passed" in result.detail
    assert client.calls == [
        (
            "http://openmerge-shared-litellm:4000/v1",
            generated.virtual_keys["dokploy-ai"],
            "opencode-go/deepseek-v4.1-flash",
        )
    ]
    assert generated.virtual_keys["dokploy-ai"] not in result.detail


def test_verify_dokploy_ai_provider_fails_on_test_connection_error_without_leaking_key() -> None:
    from dokploy_wizard.dokploy.client import DokployAiProvider
    from dokploy_wizard.dokploy.shared_core import verify_dokploy_ai_provider

    class FailingConnectionClient(FakeDokployApiClient):
        def ai_provider_test_connection(self, *, api_url: str, api_key: str, model: str):  # type: ignore[no-untyped-def]
            del api_url, model
            raise RuntimeError(f"fetch failed for {api_key}")

    generated = _generated_keys()
    client = FailingConnectionClient()
    client._ai_providers = [
        DokployAiProvider(
            ai_id="ai-wizard-existing",
            name="Dokploy Wizard LiteLLM",
            api_url="http://openmerge-shared-litellm:4000/v1",
            api_key=generated.virtual_keys["dokploy-ai"],
            model="opencode-go/deepseek-v4.1-flash",
            is_enabled=True,
        )
    ]

    result = verify_dokploy_ai_provider(
        client,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )

    assert result.passed is False
    assert "test_connection_error" in result.detail
    assert "fetch failed" in result.detail
    assert generated.virtual_keys["dokploy-ai"] not in result.detail


_MODEL_SYNC_ENV = {
    "LITELLM_OPENROUTER_API_KEY": "openrouter-upstream-key",
    "LITELLM_OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
    "LITELLM_NVIDIA_API_KEY": "nvidia-upstream-key",
    "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
}


def test_rendered_compose_includes_model_sync_sidecar() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(plan, {}, dict(_MODEL_SYNC_ENV))
    compose = rendered.compose_file

    assert "  wizard-stack-shared-litellm-sync:\n" in compose
    assert "image: python:3.12-alpine3.20" in compose
    assert 'LITELLM_GATEWAY_URL: "http://wizard-stack-shared-litellm:4000"' in compose
    assert 'MODEL_SYNC_INTERVAL_MINUTES: "360"' in compose
    assert "target: /app/model_sync.py" in compose
    assert "target: /app/sources.json" in compose
    assert re.search(r"source: wizard-stack-shared-litellm-sync-sources-[0-9a-f]{12}", compose)
    assert re.search(r"source: wizard-stack-shared-litellm-sync-script-[0-9a-f]{12}", compose)
    assert "wizard-stack-shared-litellm-sync-state:/state" in compose
    assert "condition: service_healthy" in compose
    assert '"CMD", "python3", "/app/model_sync.py", "--healthcheck"' in compose
    assert "store_model_in_db: true" in compose
    assert '"name": "openrouter"' in compose
    assert '"name": "opencode-go"' in compose
    assert '"name": "nvidia"' in compose
    assert '"cost_catalog_provider": "nvidia"' in compose
    assert '"cost_catalog_provider": "opencode-go"' in compose
    assert "openrouter-upstream-key" not in compose


def test_rendered_compose_model_sync_disabled_by_env() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan, {}, {**_MODEL_SYNC_ENV, "LITELLM_MODEL_SYNC_ENABLED": "0"}
    )
    compose = rendered.compose_file

    assert "wizard-stack-shared-litellm-sync" not in compose
    assert "store_model_in_db" not in compose


def test_rendered_compose_model_sync_absent_without_provider_keys() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(plan, {}, {})
    compose = rendered.compose_file

    assert "wizard-stack-shared-litellm-sync" not in compose
    assert "store_model_in_db" not in compose


def test_rendered_compose_model_sync_honors_provider_subset_and_interval() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_OPENROUTER_API_KEY": "openrouter-upstream-key",
            "LITELLM_OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
            "LITELLM_MODEL_SYNC_PROVIDERS": "openrouter",
            "LITELLM_MODEL_SYNC_INTERVAL_MINUTES": "30",
        },
    )
    compose = rendered.compose_file

    assert 'MODEL_SYNC_INTERVAL_MINUTES: "30"' in compose
    assert '"name": "openrouter"' in compose
    assert '"name": "opencode-go"' not in compose


def test_consumer_virtual_keys_receive_sync_wildcard() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())
    allowlists = build_litellm_consumer_model_allowlists(flat_env=dict(_MODEL_SYNC_ENV), plan=plan)

    key_lists = shared_core_module._litellm_key_model_allowlists(
        allowlists, litellm_env=dict(_MODEL_SYNC_ENV)
    )

    assert all(consumer in key_lists for consumer in allowlists)
    assert key_lists["coder-hermes"][-1] == "sync/*"
    # The wildcard reaches every consumer key, including the two built-in AI
    # surfaces; the provider-row seeder strips it rather than making a "sync/*" row.
    assert key_lists["dokploy-ai"][-1] == "sync/*"
    assert key_lists["nextcloud"][-1] == "sync/*"
    assert "sync/*" not in allowlists["coder-hermes"]

    disabled = shared_core_module._litellm_key_model_allowlists(
        allowlists, litellm_env={**_MODEL_SYNC_ENV, "LITELLM_MODEL_SYNC_ENABLED": "0"}
    )
    assert all("sync/*" not in models for models in disabled.values())


def test_push_model_sync_consumer_keys_writes_state_file_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_cmd: list[str] = []
    captured_input: list[str] = []

    def fake_run(
        cmd: list[str],
        *args: object,
        input: str | None = None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        captured_input.append(input or "")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dokploy_wizard.dokploy.shared_core.subprocess.run", fake_run)

    assert shared_core_module._push_model_sync_consumer_keys(
        "sync-container", {"coder-hermes": "sk-1"}
    )
    assert captured_cmd[:4] == ["docker", "exec", "-i", "sync-container"]
    assert json.loads(captured_input[0]) == {"coder-hermes": "sk-1"}
    shell = " ".join(captured_cmd)
    assert "consumer-keys.json.tmp" in shell and "mv" in shell


def test_push_model_sync_consumer_keys_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("dokploy_wizard.dokploy.shared_core.subprocess.run", fake_run)

    assert not shared_core_module._push_model_sync_consumer_keys("sync-container", {"a": "sk-a"})


def test_model_sync_first_cycle_completed_polls_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter([1, 1, 0])

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[-3:] == ["python3", "/app/model_sync.py", "--healthcheck"]
        return subprocess.CompletedProcess(args=cmd, returncode=next(results), stdout="", stderr="")

    monkeypatch.setattr("dokploy_wizard.dokploy.shared_core.subprocess.run", fake_run)
    sleeps: list[float] = []

    assert shared_core_module._model_sync_first_cycle_completed(
        "sync-container", attempts=5, delay_seconds=5.0, sleep_fn=sleeps.append
    )
    assert sleeps == [5.0, 5.0]

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.shared_core.subprocess.run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=""),
    )
    assert not shared_core_module._model_sync_first_cycle_completed(
        "sync-container", attempts=2, delay_seconds=0.0, sleep_fn=lambda _s: None
    )


def _ai_row(
    *,
    ai_id: str,
    name: str = "Dokploy Wizard LiteLLM",
    model: str = "opencode-go/deepseek-v4.1-flash",
) -> dict[str, object]:
    return {
        "aiId": ai_id,
        "name": name,
        "apiUrl": "http://openmerge-shared-litellm:4000/v1",
        "model": model,
        "isEnabled": True,
    }


def test_ai_provider_create_recovers_record_from_empty_mutation_response() -> None:
    """Upstream ai.create answers [] after inserting the row; the client must
    recover the record from ai.getAll instead of failing."""
    from urllib import request

    from dokploy_wizard.dokploy.client import DokployApiClient

    calls: list[str] = []

    def fake_request(req: object) -> object:
        assert isinstance(req, request.Request)
        path = req.full_url.removeprefix("http://dokploy.test")
        calls.append(path)
        if path == "/api/ai.create":
            return []
        return [_ai_row(ai_id="ai-real-1")]

    client = DokployApiClient(
        api_url="http://dokploy.test",
        api_key="fake-key",
        request_fn=fake_request,
    )

    provider = client.ai_provider_create(
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-ai-key",
        model="opencode-go/deepseek-v4.1-flash",
        is_enabled=True,
    )

    assert provider.ai_id == "ai-real-1"
    assert provider.api_key == ""
    assert calls == ["/api/ai.create", "/api/ai.getAll"]


def test_ai_provider_create_recovers_record_from_scalar_mutation_response() -> None:
    from urllib import request

    from dokploy_wizard.dokploy.client import DokployApiClient

    def fake_request(req: object) -> object:
        assert isinstance(req, request.Request)
        if req.full_url.endswith("/api/ai.create"):
            return True
        return [_ai_row(ai_id="ai-real-2")]

    client = DokployApiClient(
        api_url="http://dokploy.test",
        api_key="fake-key",
        request_fn=fake_request,
    )

    provider = client.ai_provider_create(
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-ai-key",
        model="opencode-go/deepseek-v4.1-flash",
        is_enabled=True,
    )

    assert provider.ai_id == "ai-real-2"


def test_ai_provider_update_recovers_record_from_empty_mutation_response() -> None:
    from urllib import request

    from dokploy_wizard.dokploy.client import DokployApiClient

    calls: list[str] = []

    def fake_request(req: object) -> object:
        assert isinstance(req, request.Request)
        path = req.full_url.removeprefix("http://dokploy.test")
        calls.append(path)
        if path == "/api/ai.update":
            return []
        return [_ai_row(ai_id="ai-real-3")]

    client = DokployApiClient(
        api_url="http://dokploy.test",
        api_key="fake-key",
        request_fn=fake_request,
    )

    provider = client.ai_provider_update(
        ai_id="ai-real-3",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-ai-key",
        model="opencode-go/deepseek-v4.1-flash",
        is_enabled=True,
    )

    assert provider.ai_id == "ai-real-3"
    assert calls == ["/api/ai.update", "/api/ai.getAll"]


def test_ai_provider_mutation_recovery_requires_single_match() -> None:
    from urllib import request

    import pytest

    from dokploy_wizard.dokploy.client import DokployApiClient, DokployApiError

    rows = [_ai_row(ai_id="ai-dup-1"), _ai_row(ai_id="ai-dup-2")]

    def fake_request(req: object) -> object:
        assert isinstance(req, request.Request)
        if req.full_url.endswith("/api/ai.create"):
            return []
        return rows

    client = DokployApiClient(
        api_url="http://dokploy.test",
        api_key="fake-key",
        request_fn=fake_request,
    )

    with pytest.raises(DokployApiError, match="recovery found 2 matching records"):

        client.ai_provider_create(
            name="Dokploy Wizard LiteLLM",
            api_url="http://openmerge-shared-litellm:4000/v1",
            api_key="sk-ai-key",
            model="opencode-go/deepseek-v4.1-flash",
            is_enabled=True,
        )
