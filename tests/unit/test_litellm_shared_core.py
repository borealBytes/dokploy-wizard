# ruff: noqa: E501

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

import dokploy_wizard.dokploy.shared_core as shared_core_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.core.reconciler import build_shared_core_ledger
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
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-upstream-key",
        },
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    assert isinstance(rendered, RenderedCompose)
    assert "  wizard-stack-shared-litellm:\n" in compose
    assert "image: ghcr.io/berriai/litellm:main-v1.40.14-stable" in compose
    assert "image: ghcr.io/berriai/litellm:latest" not in compose
    assert 'DATABASE_URL: "postgresql://wizard_stack_litellm:${WIZARD_STACK_LITELLM_POSTGRES_PASSWORD:?WIZARD_STACK_LITELLM_POSTGRES_PASSWORD is required}@wizard-stack-shared-postgres:5432/wizard_stack_litellm"' in compose
    assert 'LITELLM_MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'MASTER_KEY: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"' in compose
    assert 'LITELLM_SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert 'SALT_KEY: "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}"' in compose
    assert "healthcheck:\n" in compose
    assert re.search(r"source: wizard-stack-shared-litellm-config-[0-9a-f]{12}", compose)
    assert "target: /app/config.yaml" in compose
    assert 'api_key: "os.environ/LITELLM_LOCAL_API_KEY"' in compose
    assert 'model_name: "opencode-go/minimax-m2.7"' in compose
    assert 'model_name: "opencode-go/deepseek-v4-flash"' in compose
    assert 'model_name: "opencode-go/mimo-v2.5"' in compose
    assert 'model_name: "openrouter/hunter-alpha"' in compose
    assert '      LITELLM_OPENCODE_GO_API_KEY: "${LITELLM_OPENCODE_GO_API_KEY:?LITELLM_OPENCODE_GO_API_KEY is required}"' in compose
    assert (
        '      LITELLM_OPENROUTER_API_KEY: "${LITELLM_OPENROUTER_API_KEY:?LITELLM_OPENROUTER_API_KEY is required}"'
        in compose
    )
    assert env_specs["LITELLM_OPENCODE_GO_API_KEY"].value == "opencode-go-upstream-key"
    assert env_specs["LITELLM_OPENROUTER_API_KEY"].value == "farm-openrouter-upstream-key"
    assert env_specs["LITELLM_LOCAL_API_KEY"].target_services == ("wizard-stack-shared-litellm",)
    assert env_specs["LITELLM_MASTER_KEY"].target_services == ("wizard-stack-shared-litellm",)
    assert env_specs["LITELLM_SALT_KEY"].target_services == ("wizard-stack-shared-litellm",)
    assert env_specs["WIZARD_STACK_LITELLM_POSTGRES_PASSWORD"].target_services == (
        "wizard-stack-shared-postgres",
        "wizard-stack-shared-litellm",
    )
    assert "opencode-go-upstream-key" not in compose
    assert "farm-openrouter-upstream-key" not in compose
    assert "    aliases:\n          - wizard-stack-shared-litellm\n" in compose
    assert '      - "127.0.0.1:4000:4000"' in compose
    assert "    expose:\n" not in compose


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
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "legacy-openrouter-key",
        },
    )
    compose = rendered.compose_file
    env_specs = {spec.name: spec for spec in rendered.env_specs}

    assert '      LITELLM_OPENCODE_GO_API_KEY: "${LITELLM_OPENCODE_GO_API_KEY:?LITELLM_OPENCODE_GO_API_KEY is required}"' in compose
    assert '      LITELLM_OPENROUTER_API_KEY: "${LITELLM_OPENROUTER_API_KEY:?LITELLM_OPENROUTER_API_KEY is required}"' in compose
    assert '      LITELLM_OPENCODE_GO_API_KEY: "${OPENCODE_GO_API_KEY}' not in compose
    assert (
        '      LITELLM_OPENROUTER_API_KEY: "${MY_FARM_ADVISOR_OPENROUTER_API_KEY'
        not in compose
    )
    assert env_specs["LITELLM_OPENCODE_GO_API_KEY"].value == "litellm-opencode-key"
    assert env_specs["LITELLM_OPENROUTER_API_KEY"].value == "litellm-openrouter-key"
    assert 'model_name: "opencode-go/minimax-m2.7"' in compose
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
        rendered_compose=rendered_compose.compose_file,
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
        "openmerge-shared-yqjzwd-openmerge-shared-postgres-1"
    ]
    assert redis_container_names == ["openmerge-shared-yqjzwd-openmerge-shared-redis-1"]


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

    assert allowlists["my-farm-advisor"] == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    )
    assert allowlists["openclaw"] == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    )


class _FakeLiteLLMAdminApi:
    def __init__(self, readiness_payload: dict[str, object]) -> None:
        self._readiness_payload = readiness_payload
        self.readiness_calls = 0

    def readiness(self) -> dict[str, object]:
        self.readiness_calls += 1
        return dict(self._readiness_payload)

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return ()

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        del metadata
        return LiteLLMTeamRecord(team_id=f"team-{team_alias}", team_alias=team_alias, models=models)

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        del team_id, metadata
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

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
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
        master_key="sk-master-fake-test-key",
        salt_key="sk-salt-fake-test-key",
        virtual_keys={
            "coder-hermes": "sk-hermes-fake-test-key",
            "coder-kdense": "sk-kdense-fake-test-key",
            "my-farm-advisor": "sk-farm-fake-test-key",
            "openclaw": "sk-openclaw-fake-test-key",
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


def test_ai_provider_create_records_payload_on_fake() -> None:
    from tests.unit.fake_dokploy import FakeDokployApiClient

    fake = FakeDokployApiClient()
    fake.ai_provider_create(
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        is_enabled=True,
    )

    assert len(fake._ai_provider_creates) == 1
    create = fake._ai_provider_creates[0]
    assert create["name"] == "Dokploy Wizard LiteLLM"
    assert create["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    assert create["apiKey"] == "sk-master-fake-test-key"
    assert create["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    assert create["isEnabled"] is True


def test_ai_provider_update_records_payload_on_fake() -> None:
    from tests.unit.fake_dokploy import FakeDokployApiClient

    fake = FakeDokployApiClient()
    fake.ai_provider_update(
        ai_id="ai-wizard-1",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        is_enabled=True,
    )

    assert len(fake._ai_provider_updates) == 1
    update = fake._ai_provider_updates[0]
    assert update["aiId"] == "ai-wizard-1"
    assert update["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"


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
        if method == "POST":
            return {
                "data": {
                    "aiId": "ai-fake-1",
                    "name": "Dokploy Wizard LiteLLM",
                    "apiUrl": "http://openmerge-shared-litellm:4000/v1",
                    "apiKey": "sk-master-fake-test-key",
                    "model": "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
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
        model="tuxdesktop.tailb12aa5.ts.net/unsloth-active",
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
            "model": "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            "isEnabled": True,
        },
    )

    client.ai_provider_update(
        ai_id="ai-fake-1",
        name="Dokploy Wizard LiteLLM",
        api_url="http://openmerge-shared-litellm:4000/v1",
        api_key="sk-master-fake-test-key",
        model="tuxdesktop.tailb12aa5.ts.net/unsloth-active",
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
            "model": "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            "isEnabled": True,
        },
    )


def test_ai_model_alias_default_with_no_env() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    assert _dokploy_ai_model_alias({}) == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"


def test_ai_model_alias_custom_provider_with_dot() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "custom.example.internal",
        "AI_DEFAULT_MODEL": "agent-model",
    })
    assert result == "custom.example.internal/agent-model"


def test_ai_model_alias_local_provider_maps_to_default_local() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    result = _dokploy_ai_model_alias({
        "AI_DEFAULT_PROVIDER": "local",
        "AI_DEFAULT_MODEL": "custom-model",
    })
    assert result.startswith("tuxdesktop.tailb12aa5.ts.net/")
    assert result == "tuxdesktop.tailb12aa5.ts.net/custom-model"


def test_ai_model_alias_partial_env_falls_back_to_default() -> None:
    from dokploy_wizard.dokploy.shared_core import _dokploy_ai_model_alias

    assert _dokploy_ai_model_alias({"AI_DEFAULT_PROVIDER": "local"}) == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    assert _dokploy_ai_model_alias({"AI_DEFAULT_MODEL": "model-x"}) == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"


def test_ai_reconcile_creates_provider_when_missing() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )

    assert len(fake._ai_provider_creates) == 1
    assert len(fake._ai_provider_updates) == 0
    assert fake._ai_provider_creates[0]["name"] == "Dokploy Wizard LiteLLM"
    assert fake._ai_provider_creates[0]["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    assert fake._ai_provider_creates[0]["apiKey"] == "sk-master-fake-test-key"
    assert fake._ai_provider_creates[0]["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    assert fake._ai_provider_creates[0]["isEnabled"] is True


def test_ai_reconcile_updates_provider_when_present() -> None:
    from dokploy_wizard.dokploy.client import DokployAiProvider
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    existing = DokployAiProvider(
        ai_id="ai-wizard-existing",
        name="Dokploy Wizard LiteLLM",
        api_url="http://old-url:4000/v1",
        api_key="sk-old-key",
        model="bare-model",
        is_enabled=True,
    )
    fake._ai_providers = [existing]
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
    )

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 1
    update = fake._ai_provider_updates[0]
    assert update["aiId"] == "ai-wizard-existing"
    assert update["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"


def test_ai_reconcile_does_not_create_duplicate_on_repeat() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(client=fake, litellm_service_name="openmerge-shared-litellm", generated_keys=generated)
    _ensure_dokploy_ai_provider(client=fake, litellm_service_name="openmerge-shared-litellm", generated_keys=generated)
    _ensure_dokploy_ai_provider(client=fake, litellm_service_name="openmerge-shared-litellm", generated_keys=generated)

    assert len(fake._ai_provider_creates) == 1
    assert len(fake._ai_provider_updates) == 2


def test_ai_reconcile_uses_canonical_model_alias_not_bare() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(client=fake, litellm_service_name="openmerge-shared-litellm", generated_keys=generated)

    for create in fake._ai_provider_creates:
        assert create["model"] != "unsloth-active"
        assert create["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
    for update in fake._ai_provider_updates:
        assert update["model"] != "unsloth-active"
        assert update["model"] == "tuxdesktop.tailb12aa5.ts.net/unsloth-active"


def test_ai_reconcile_skips_when_no_generated_keys() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()

    _ensure_dokploy_ai_provider(client=fake, litellm_service_name="openmerge-shared-litellm", generated_keys=None)

    assert len(fake._ai_provider_creates) == 0
    assert len(fake._ai_provider_updates) == 0


def test_ai_reconcile_uses_internal_litellm_url() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name="openmerge-shared-litellm",
        generated_keys=generated,
        litellm_env={},
    )

    assert fake._ai_provider_creates[0]["apiUrl"] == "http://openmerge-shared-litellm:4000/v1"
    api_url = fake._ai_provider_creates[0]["apiUrl"]
    assert isinstance(api_url, str)
    assert "api.openai.com" not in api_url


def test_ai_reconcile_uses_custom_provider_alias() -> None:
    from dokploy_wizard.dokploy.shared_core import _ensure_dokploy_ai_provider

    fake = FakeDokployApiClient()
    generated = _generated_keys()

    _ensure_dokploy_ai_provider(
        client=fake,
        litellm_service_name="my-stack-shared-litellm",
        generated_keys=generated,
        litellm_env={
            "AI_DEFAULT_PROVIDER": "custom.example.internal",
            "AI_DEFAULT_MODEL": "agent-model",
        },
    )

    assert len(fake._ai_provider_creates) == 1
    assert fake._ai_provider_creates[0]["model"] == "custom.example.internal/agent-model"
    assert fake._ai_provider_creates[0]["apiUrl"] == "http://my-stack-shared-litellm:4000/v1"
