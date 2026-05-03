# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.state import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state
from dokploy_wizard.uninstall import (
    UninstallConfirmationError,
    UninstallPlanningError,
    build_pack_disable_plan,
    build_uninstall_plan,
    collect_confirmation_lines,
    compute_remaining_completed_steps,
)


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _desired(*, nextcloud: bool = False, matrix: bool = False, coder: bool = False) -> DesiredState:
    from dokploy_wizard.state import resolve_desired_state

    values = {
        "STACK_NAME": "nextcloud-stack",
        "ROOT_DOMAIN": "example.com",
        "ENABLE_HEADSCALE": "true",
    }
    if matrix:
        values["ENABLE_MATRIX"] = "true"
    if nextcloud:
        values["ENABLE_NEXTCLOUD"] = "true"
    if coder:
        values["ENABLE_CODER"] = "true"
    values["ENABLE_OPENCLAW"] = "true"
    return resolve_desired_state(_raw(values))


def test_uninstall_plan_retains_data_by_default() -> None:
    desired = _desired(nextcloud=True, coder=True)
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "cloudflare_access_otp_provider",
                "otp-provider-1",
                "account:one:access-otp-provider",
            ),
            OwnedResource(
                "cloudflare_access_application",
                "app-openclaw",
                "account:one:access-app:openclaw.example.com",
            ),
            OwnedResource(
                "cloudflare_access_policy",
                "policy-openclaw",
                "account:one:access-policy:openclaw.example.com",
            ),
            OwnedResource("cloudflare_dns_record", "dns-dokploy", "zone:dokploy"),
            OwnedResource("cloudflare_tunnel", "tunnel-1", "account:one"),
            OwnedResource(
                "shared_core_network", "network-1", "stack:nextcloud-stack:shared-network"
            ),
            OwnedResource(
                "shared_core_postgres", "postgres-1", "stack:nextcloud-stack:shared-postgres"
            ),
            OwnedResource("shared_core_redis", "redis-1", "stack:nextcloud-stack:shared-redis"),
            OwnedResource(
                "shared_core_litellm", "litellm-1", "stack:nextcloud-stack:shared-litellm"
            ),
            OwnedResource("headscale_service", "headscale-1", "stack:nextcloud-stack:headscale"),
            OwnedResource(
                "nextcloud_service", "nextcloud-1", "stack:nextcloud-stack:nextcloud-service"
            ),
            OwnedResource(
                "onlyoffice_service", "onlyoffice-1", "stack:nextcloud-stack:onlyoffice-service"
            ),
            OwnedResource(
                "nextcloud_volume", "nextcloud-volume", "stack:nextcloud-stack:nextcloud-volume"
            ),
            OwnedResource(
                "onlyoffice_volume", "onlyoffice-volume", "stack:nextcloud-stack:onlyoffice-volume"
            ),
            OwnedResource("coder_service", "coder-1", "stack:nextcloud-stack:coder:service"),
            OwnedResource("coder_data", "coder-data", "stack:nextcloud-stack:coder:data"),
            OwnedResource(
                "openclaw_service",
                "openclaw-1",
                "stack:nextcloud-stack:openclaw",
            ),
            OwnedResource(
                "openclaw_mem0_service",
                "mem0-1",
                "stack:nextcloud-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                "openclaw_qdrant_service",
                "qdrant-1",
                "stack:nextcloud-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                "openclaw_runtime_service",
                "nexa-runtime-1",
                "stack:nextcloud-stack:openclaw-sidecar:nexa-runtime",
            ),
        ),
    )

    plan = build_uninstall_plan(
        raw_input=_raw({"STACK_NAME": "nextcloud-stack"}),
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=False,
    )

    assert plan.mode == "retain"
    assert [item.resource.resource_type for item in plan.deletions] == [
        "cloudflare_access_otp_provider",
        "cloudflare_access_application",
        "cloudflare_access_policy",
        "openclaw_service",
        "openclaw_mem0_service",
        "openclaw_qdrant_service",
        "openclaw_runtime_service",
        "nextcloud_service",
        "onlyoffice_service",
        "coder_service",
        "headscale_service",
        "shared_core_litellm",
        "shared_core_network",
        "cloudflare_dns_record",
        "cloudflare_tunnel",
    ]
    assert [resource.resource_type for resource in plan.retained_resources] == [
        "coder_data",
        "nextcloud_volume",
        "onlyoffice_volume",
        "shared_core_postgres",
        "shared_core_redis",
    ]


def test_uninstall_plan_rejects_unknown_ledger_resource_type() -> None:
    with pytest.raises(UninstallPlanningError, match="unsupported resource type 'mystery'"):
        build_uninstall_plan(
            raw_input=_raw({"STACK_NAME": "wizard-stack"}),
            desired_state=_desired(),
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(OwnedResource("mystery", "id-1", "scope-1"),),
            ),
            destroy_data=False,
        )


def test_compute_remaining_completed_steps_shrinks_after_runtime_delete() -> None:
    desired = _desired(nextcloud=True)
    remaining = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "shared_core_postgres", "postgres-1", "stack:nextcloud-stack:shared-postgres"
            ),
            OwnedResource("shared_core_redis", "redis-1", "stack:nextcloud-stack:shared-redis"),
            OwnedResource(
                "shared_core_litellm", "litellm-1", "stack:nextcloud-stack:shared-litellm"
            ),
            OwnedResource("openclaw_service", "openclaw-1", "stack:nextcloud-stack:openclaw"),
            OwnedResource(
                "openclaw_mem0_service",
                "mem0-1",
                "stack:nextcloud-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                "openclaw_qdrant_service",
                "qdrant-1",
                "stack:nextcloud-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                "openclaw_runtime_service",
                "nexa-runtime-1",
                "stack:nextcloud-stack:openclaw-sidecar:nexa-runtime",
            ),
            OwnedResource(
                "nextcloud_volume", "nextcloud-volume", "stack:nextcloud-stack:nextcloud-volume"
            ),
            OwnedResource(
                "onlyoffice_volume", "onlyoffice-volume", "stack:nextcloud-stack:onlyoffice-volume"
            ),
        ),
    )

    completed = compute_remaining_completed_steps(
        desired_state=desired,
        raw_input=_raw({"STACK_NAME": "nextcloud-stack"}),
        ownership_ledger=remaining,
    )

    assert completed == ("preflight", "dokploy_bootstrap")


def test_build_pack_disable_plan_deletes_tailscale_node_even_without_pack_removal() -> None:
    existing = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_TAILSCALE": "true",
                "TAILSCALE_AUTH_KEY": "tskey-auth-123",
                "TAILSCALE_HOSTNAME": "wizard-admin",
            },
        )
    )
    requested = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            },
        )
    )
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("tailscale_node", "wizard-admin", "stack:wizard-stack:tailscale"),
        ),
    )

    plan = build_pack_disable_plan(
        existing_desired=existing,
        requested_desired=requested,
        ownership_ledger=ledger,
    )

    assert [item.resource.resource_type for item in plan.deletions] == ["tailscale_node"]


def test_build_pack_disable_plan_caps_checkpoint_before_shared_core_for_litellm_consumer_removal() -> None:
    existing = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
            },
        )
    )
    requested = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
            },
        )
    )

    plan = build_pack_disable_plan(
        existing_desired=existing,
        requested_desired=requested,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
    )

    assert plan.completed_steps_ceiling == ("preflight", "dokploy_bootstrap", "networking")


def test_uninstall_plan_retains_litellm_shared_core_for_no_ai_pack_install() -> None:
    desired = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            },
        )
    )
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("shared_core_network", "network-1", "stack:wizard-stack:shared-network"),
            OwnedResource("shared_core_postgres", "postgres-1", "stack:wizard-stack:shared-postgres"),
        ),
    )

    plan = build_uninstall_plan(
        raw_input=_raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"}),
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=False,
    )

    assert [item.resource.resource_type for item in plan.deletions] == ["shared_core_network"]
    assert [resource.resource_type for resource in plan.retained_resources] == ["shared_core_postgres"]


def test_destroy_confirmation_requires_three_strong_lines(tmp_path: Path) -> None:
    confirm_file = tmp_path / "destroy.confirm"
    confirm_file.write_text(
        "I understand this is destructive\n"
        "Destroy data now\n"
        "Destroy all data for nextcloud-stack\n",
        encoding="utf-8",
    )

    lines = collect_confirmation_lines(
        non_interactive=True,
        confirm_file=confirm_file,
        mode="destroy",
        environment="nextcloud-stack",
    )

    assert len(lines) == 3


def test_destroy_confirmation_rejects_weak_phrase(tmp_path: Path) -> None:
    confirm_file = tmp_path / "weak.confirm"
    confirm_file.write_text("yes\n", encoding="utf-8")

    with pytest.raises(UninstallConfirmationError, match="Weak confirmation phrases"):
        collect_confirmation_lines(
            non_interactive=True,
            confirm_file=confirm_file,
            mode="destroy",
            environment="nextcloud-stack",
        )
