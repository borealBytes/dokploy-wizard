# ruff: noqa: E501
from __future__ import annotations

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.core.reconciler import build_shared_core_ledger
from dokploy_wizard.dokploy.shared_core import _render_compose_file
from dokploy_wizard.state.models import OwnedResource, OwnershipLedger


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
    assert plan.litellm.default_model_alias_order == (
        "local/unsloth-active",
        "opencode-go/*",
        "openrouter/auto",
        "openrouter/openrouter/free",
    )


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
            "LITELLM_LOCAL_MODEL": "unsloth/Qwen2.5-Coder-32B-Instruct",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
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
    assert 'MASTER_KEY: "${LITELLM_MASTER_KEY}"' in rendered
    assert 'SALT_KEY: "${LITELLM_SALT_KEY}"' in rendered
    assert "healthcheck:\n" in rendered
    assert "source: wizard-stack-shared-litellm-config" in rendered
    assert "target: /app/config.yaml" in rendered
    assert "os.environ/MY_FARM_ADVISOR_OPENROUTER_API_KEY" in rendered
    assert "os.environ/OPENCODE_GO_API_KEY" in rendered
    assert "    aliases:\n          - wizard-stack-shared-litellm\n" in rendered
    assert '      - "127.0.0.1:4000:4000"' in rendered
    assert "    expose:\n" not in rendered


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
