# ruff: noqa: E501
from __future__ import annotations

import re

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.core.reconciler import build_shared_core_ledger
from dokploy_wizard.dokploy.shared_core import _render_compose_file
from dokploy_wizard.state.models import (
    STATE_FORMAT_VERSION,
    LiteLLMGeneratedKeys,
    OwnedResource,
    OwnershipLedger,
)


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
