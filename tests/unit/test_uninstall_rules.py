# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.state import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput
from dokploy_wizard.uninstall import (
    UninstallConfirmationError,
    UninstallPlanningError,
    build_uninstall_plan,
    collect_confirmation_lines,
    compute_remaining_completed_steps,
)


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _desired(*, nextcloud: bool = False, matrix: bool = False) -> DesiredState:
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
    return resolve_desired_state(_raw(values))


def test_uninstall_plan_retains_data_by_default() -> None:
    desired = _desired(nextcloud=True)
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
        "nextcloud_service",
        "onlyoffice_service",
        "headscale_service",
        "shared_core_network",
        "cloudflare_dns_record",
        "cloudflare_tunnel",
    ]
    assert [resource.resource_type for resource in plan.retained_resources] == [
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
