# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

import dokploy_wizard.dokploy.shared_core as shared_core_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.core.reconciler import build_shared_core_ledger
from dokploy_wizard.dokploy.shared_core import (
    DokploySharedCoreBackend,
    _render_compose_file,
    build_litellm_consumer_model_allowlists,
)
from dokploy_wizard.litellm.admin import LiteLLMTeamRecord, LiteLLMVirtualKeyRecord
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    write_applied_checkpoint,
)
from dokploy_wizard.state.models import (
    STATE_FORMAT_VERSION,
    LiteLLMGeneratedKeys,
    OwnedResource,
    OwnershipLedger,
)

from .fake_dokploy import FakeDokployApiClient


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
    assert plan.litellm.default_model_alias_order == ("local/unsloth-active",)


def test_litellm_db_allocation_is_dedicated_and_not_a_pack_allocation() -> None:
    plan = build_shared_core_plan(stack_name="openmerge", enabled_packs=("nextcloud", "openclaw"))

    assert plan.litellm is not None
    assert [allocation.pack_name for allocation in plan.allocations] == ["nextcloud", "openclaw"]
    assert all(allocation.pack_name != "litellm" for allocation in plan.allocations)
    assert all(allocation.postgres != plan.litellm.postgres for allocation in plan.allocations)
    assert plan.litellm.postgres == SharedPostgresAllocation(
        database_name="openmerge_litellm",
        user_name="openmerge_litellm",
        password_secret_ref="openmerge-litellm-postgres-password",
    )


def test_rendered_compose_includes_pinned_litellm_service() -> None:
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
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
        },
    )

    assert "  wizard-stack-shared-litellm:\n" in rendered
    assert "image: ghcr.io/berriai/litellm:main-v1.40.14-stable" in rendered
    assert "image: ghcr.io/berriai/litellm:latest" not in rendered
    assert 'DATABASE_URL: "postgresql://wizard_stack_litellm:${WIZARD_STACK_LITELLM_POSTGRES_PASSWORD:-change-me}@wizard-stack-shared-postgres:5432/wizard_stack_litellm"' in rendered
    assert 'LITELLM_MASTER_KEY: "${LITELLM_MASTER_KEY}"' in rendered
    assert 'MASTER_KEY: "${LITELLM_MASTER_KEY}"' in rendered
    assert 'LITELLM_SALT_KEY: "${LITELLM_SALT_KEY}"' in rendered
    assert 'SALT_KEY: "${LITELLM_SALT_KEY}"' in rendered
    assert "healthcheck:\n" in rendered
    assert re.search(r"source: wizard-stack-shared-litellm-config-[0-9a-f]{12}", rendered)
    assert "target: /app/config.yaml" in rendered
    assert 'api_key: "sk-no-key-required"' in rendered
    assert 'model_name: "openai/*"' not in rendered
    assert 'OPENCODE_GO_API_KEY: "opencode-go-upstream-key"' not in rendered
    assert 'MY_FARM_ADVISOR_OPENROUTER_API_KEY: "farm-openrouter-upstream-key"' not in rendered
    assert 'api_key: "opencode-go-upstream-key"' not in rendered
    assert 'api_key: "farm-openrouter-upstream-key"' not in rendered
    assert "    aliases:\n          - wizard-stack-shared-litellm\n" in rendered
    assert '      - "127.0.0.1:4000:4000"' in rendered
    assert "    expose:\n" not in rendered


def test_rendered_compose_keeps_only_local_route_when_non_local_routes_paused() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-no-key-required",
            "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free="
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
            ),
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
            "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
            "OPENCLAW_NVIDIA_API_KEY": "openclaw-nvidia-upstream-key",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
    )

    assert 'model_name: "local/unsloth-active"' in rendered
    assert 'model: "openai/unsloth-active"' in rendered
    assert 'api_key: "sk-no-key-required"' in rendered
    assert 'model_name: "openai/*"' not in rendered
    assert 'openrouter/nvidia/nemotron-3-super-120b-a12b:free' not in rendered
    assert 'OPENCODE_GO_API_KEY: "opencode-go-upstream-key"' not in rendered
    assert 'MY_FARM_ADVISOR_OPENROUTER_API_KEY: "farm-openrouter-upstream-key"' not in rendered


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
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "sk-local-override",
        },
    )

    first_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})", rendered_first
    )
    second_name = re.search(
        r"source: (wizard-stack-shared-litellm-config-[0-9a-f]{12})", rendered_second
    )

    assert first_name is not None
    assert second_name is not None
    assert first_name.group(1) != second_name.group(1)


def test_rendered_compose_inlines_documented_and_legacy_litellm_keys_when_generated() -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    rendered = _render_compose_file(
        plan,
        {},
        {},
        litellm_generated_keys=LiteLLMGeneratedKeys(
            format_version=STATE_FORMAT_VERSION,
            master_key="sk-master-generated",
            salt_key="sk-salt-generated",
            virtual_keys={
                "coder-hermes": "sk-hermes-generated",
                "coder-kdense": "sk-kdense-generated",
                "my-farm-advisor": "sk-farm-generated",
                "openclaw": "sk-openclaw-generated",
            },
        ),
    )

    assert 'LITELLM_MASTER_KEY: "sk-master-generated"' in rendered
    assert 'MASTER_KEY: "sk-master-generated"' in rendered
    assert 'LITELLM_SALT_KEY: "sk-salt-generated"' in rendered
    assert 'SALT_KEY: "sk-salt-generated"' in rendered
    assert '${LITELLM_MASTER_KEY}' not in rendered
    assert '${LITELLM_SALT_KEY}' not in rendered


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
        compose_file=rendered_compose,
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
    assert allocation_checks == [plan.allocations[0].postgres]
    assert redis_checks == ["redis-ctr"]
    assert container_lookups.count(plan.postgres.service_name) == 1
    assert container_lookups.count(plan.redis.service_name) == 1


def test_shared_core_unhealthy_litellm_blocks_noop_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("nextcloud",))
    litellm_env = {
        "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
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
        compose_file=rendered_compose,
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


def test_litellm_allowlists_keep_local_and_bare_aliases_for_advisors() -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("my-farm-advisor", "openclaw"),
    )

    allowlists = build_litellm_consumer_model_allowlists(
        flat_env={
            "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "local/unsloth-active",
            "OPENCLAW_PRIMARY_MODEL": "local/unsloth-active",
        },
        plan=plan,
    )

    assert allowlists["my-farm-advisor"] == ("local/unsloth-active", "unsloth-active")
    assert allowlists["openclaw"] == ("local/unsloth-active", "unsloth-active")


class _FakeLiteLLMAdminApi:
    def __init__(self, readiness_payload: dict[str, object]) -> None:
        self._readiness_payload = readiness_payload
        self.readiness_calls = 0

    def readiness(self) -> dict[str, object]:
        self.readiness_calls += 1
        return dict(self._readiness_payload)

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return ()

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord:
        return LiteLLMTeamRecord(team_id=f"team-{team_alias}", team_alias=team_alias, models=models)

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return ()

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        del metadata
        return LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
        )


def _generated_keys() -> LiteLLMGeneratedKeys:
    return LiteLLMGeneratedKeys(
        format_version=STATE_FORMAT_VERSION,
        master_key="sk-master-generated",
        salt_key="sk-salt-generated",
        virtual_keys={
            "coder-hermes": "sk-hermes-generated",
            "coder-kdense": "sk-kdense-generated",
            "my-farm-advisor": "sk-farm-generated",
            "openclaw": "sk-openclaw-generated",
        },
    )


def _write_hash_checkpoint(state_dir: Path, *, service_key: str, rendered_compose: str) -> None:
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("preflight",),
            compose_artifact_hashes={
                service_key: ComposeArtifactHashState.from_rendered_compose(
                    service_id=service_key,
                    rendered_compose=rendered_compose,
                )
            },
        ),
    )
