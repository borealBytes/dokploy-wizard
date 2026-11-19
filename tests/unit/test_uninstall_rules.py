# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    resolve_desired_state,
)
from dokploy_wizard.uninstall import (
    UninstallConfirmationError,
    UninstallPlanningError,
    build_pack_disable_plan,
    build_uninstall_plan,
    collect_confirmation_lines,
    compute_remaining_completed_steps,
)
from dokploy_wizard.uninstall.planner import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
    SURFSENSE_DATA_RESOURCE_TYPE,
    SURFSENSE_SERVICE_RESOURCE_TYPE,
)


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _desired(*, matrix: bool = False, coder: bool = False) -> DesiredState:
    from dokploy_wizard.state import resolve_desired_state

    values = {
        "STACK_NAME": "nextcloud-stack",
        "ROOT_DOMAIN": "example.com",
        "ENABLE_HEADSCALE": "true",
    }
    if matrix:
        values["ENABLE_MATRIX"] = "true"
    if coder:
        values["ENABLE_CODER"] = "true"
    return resolve_desired_state(_raw(values))


def test_uninstall_plan_retains_data_by_default() -> None:
    # Legacy-ledger fixture: the Nextcloud + OnlyOffice pack (and the other retired
    # packs) are gone from the catalog, but a box whose ledger still names their
    # resource types must plan a clean uninstall, so the planner keeps the deletion
    # rules as literal insurance constants.
    desired = _desired(coder=True)
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
            OwnedResource(
                "shared_core_mail_relay", "postfix-1", "stack:nextcloud-stack:shared-postfix"
            ),
            OwnedResource("headscale_service", "headscale-1", "stack:nextcloud-stack:headscale"),
            OwnedResource(
                NEXTCLOUD_SERVICE_RESOURCE_TYPE,
                "nextcloud-1",
                "stack:nextcloud-stack:nextcloud-service",
            ),
            OwnedResource(
                ONLYOFFICE_SERVICE_RESOURCE_TYPE,
                "onlyoffice-1",
                "stack:nextcloud-stack:onlyoffice-service",
            ),
            OwnedResource(
                NEXTCLOUD_VOLUME_RESOURCE_TYPE,
                "nextcloud-volume",
                "stack:nextcloud-stack:nextcloud-volume",
            ),
            OwnedResource(
                ONLYOFFICE_VOLUME_RESOURCE_TYPE,
                "onlyoffice-volume",
                "stack:nextcloud-stack:onlyoffice-volume",
            ),
            OwnedResource("coder_service", "coder-1", "stack:nextcloud-stack:coder:service"),
            OwnedResource("coder_data", "coder-data", "stack:nextcloud-stack:coder:data"),
            OwnedResource(
                OPENCLAW_SERVICE_RESOURCE_TYPE,
                "openclaw-1",
                "stack:nextcloud-stack:openclaw",
            ),
            OwnedResource(
                OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
                "mem0-1",
                "stack:nextcloud-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
                "qdrant-1",
                "stack:nextcloud-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
                "runtime-1",
                "stack:nextcloud-stack:openclaw-sidecar:runtime",
            ),
            OwnedResource(
                MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
                "advisor-1",
                "stack:nextcloud-stack:my-farm-advisor",
            ),
            OwnedResource(
                SURFSENSE_SERVICE_RESOURCE_TYPE,
                "surfsense-1",
                "stack:nextcloud-stack:surfsense",
            ),
            OwnedResource(
                SURFSENSE_DATA_RESOURCE_TYPE,
                "surfsense-data",
                "stack:nextcloud-stack:surfsense-data",
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
        OPENCLAW_SERVICE_RESOURCE_TYPE,
        OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
        OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
        OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
        MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
        NEXTCLOUD_SERVICE_RESOURCE_TYPE,
        ONLYOFFICE_SERVICE_RESOURCE_TYPE,
        SURFSENSE_SERVICE_RESOURCE_TYPE,
        "coder_service",
        "headscale_service",
        "shared_core_litellm",
        "shared_core_network",
        "shared_core_mail_relay",
        "cloudflare_dns_record",
        "cloudflare_tunnel",
    ]
    assert [(item.phase, item.policy) for item in plan.deletions] == [
        ("cloudflare_access", "retain_safe"),
        ("cloudflare_access", "retain_safe"),
        ("cloudflare_access", "retain_safe"),
        ("openclaw", "retain_safe"),
        ("openclaw", "retain_safe"),
        ("openclaw", "retain_safe"),
        ("openclaw", "retain_safe"),
        ("my-farm-advisor", "retain_safe"),
        ("nextcloud", "retain_safe"),
        ("nextcloud", "retain_safe"),
        ("surfsense", "retain_safe"),
        ("coder", "retain_safe"),
        ("headscale", "retain_safe"),
        ("shared_core", "retain_safe"),
        ("shared_core", "retain_safe"),
        ("shared_core", "retain_safe"),
        ("networking", "retain_safe"),
        ("networking", "retain_safe"),
    ]
    assert [resource.resource_type for resource in plan.retained_resources] == [
        "coder_data",
        NEXTCLOUD_VOLUME_RESOURCE_TYPE,
        ONLYOFFICE_VOLUME_RESOURCE_TYPE,
        "shared_core_postgres",
        "shared_core_redis",
        SURFSENSE_DATA_RESOURCE_TYPE,
    ]

    destroy_plan = build_uninstall_plan(
        raw_input=_raw({"STACK_NAME": "nextcloud-stack"}),
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )
    assert destroy_plan.retained_resources == ()
    typed = {
        item.resource.resource_type: (item.phase, item.policy)
        for item in destroy_plan.deletions
    }
    assert typed[SURFSENSE_DATA_RESOURCE_TYPE] == ("surfsense", "destroy_only")
    assert typed[SURFSENSE_SERVICE_RESOURCE_TYPE] == ("surfsense", "retain_safe")
    assert typed[MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE] == ("my-farm-advisor", "retain_safe")
    assert typed[OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE] == ("openclaw", "retain_safe")


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
    desired = _desired()
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
            OwnedResource(
                OPENCLAW_SERVICE_RESOURCE_TYPE,
                "openclaw-1",
                "stack:nextcloud-stack:openclaw",
            ),
            OwnedResource(
                OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
                "mem0-1",
                "stack:nextcloud-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
                "qdrant-1",
                "stack:nextcloud-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
                "runtime-1",
                "stack:nextcloud-stack:openclaw-sidecar:runtime",
            ),
            OwnedResource(
                MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
                "advisor-1",
                "stack:nextcloud-stack:my-farm-advisor",
            ),
            OwnedResource(
                NEXTCLOUD_VOLUME_RESOURCE_TYPE,
                "nextcloud-volume",
                "stack:nextcloud-stack:nextcloud-volume",
            ),
            OwnedResource(
                ONLYOFFICE_VOLUME_RESOURCE_TYPE,
                "onlyoffice-volume",
                "stack:nextcloud-stack:onlyoffice-volume",
            ),
        ),
    )

    completed = compute_remaining_completed_steps(
        desired_state=desired,
        raw_input=_raw({"STACK_NAME": "nextcloud-stack"}),
        ownership_ledger=remaining,
    )

    assert completed == ("preflight", "dokploy_bootstrap")


def test_compute_remaining_completed_steps_still_completes_legacy_retired_pack_phases() -> None:
    # Insurance: an old box that ran the retired packs has a ledger naming their
    # resources; a desired state (reconstructed by an older wizard) that still
    # enables them must reach the full completed-steps prefix.
    requested = resolve_desired_state(
        _raw(
            {
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "true",
            }
        )
    )
    legacy = replace(
        requested,
        enabled_packs=tuple(
            sorted(set(requested.enabled_packs) | {"openclaw", "my-farm-advisor"})
        ),
    )
    legacy_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("cloudflare_tunnel", "tunnel-1", "account:one"),
            OwnedResource("cloudflare_dns_record", "dns-1", "zone:one:dokploy.example.com"),
            OwnedResource("cloudflare_dns_record", "dns-2", "zone:one:headscale.example.com"),
            OwnedResource("shared_core_network", "net-1", "stack:wizard-stack:shared-network"),
            OwnedResource("shared_core_postgres", "pg-1", "stack:wizard-stack:shared-postgres"),
            OwnedResource("shared_core_litellm", "lm-1", "stack:wizard-stack:shared-litellm"),
            OwnedResource("headscale_service", "hs-1", "stack:wizard-stack:headscale"),
            OwnedResource(
                OPENCLAW_SERVICE_RESOURCE_TYPE, "oc-1", "stack:wizard-stack:openclaw"
            ),
            OwnedResource(
                OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
                "mem0-1",
                "stack:wizard-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
                "qdrant-1",
                "stack:wizard-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
                "runtime-1",
                "stack:wizard-stack:openclaw-sidecar:runtime",
            ),
            OwnedResource(
                MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
                "advisor-1",
                "stack:wizard-stack:my-farm-advisor",
            ),
        ),
    )

    completed = compute_remaining_completed_steps(
        desired_state=legacy,
        raw_input=_raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"}),
        ownership_ledger=legacy_ledger,
    )

    assert completed == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "cloudflare_access",
        "shared_core",
        "headscale",
        "openclaw",
        "my-farm-advisor",
    )


def test_access_is_complete_only_when_the_admin_hostname_has_its_resources() -> None:
    from dokploy_wizard.networking.planner import (
        ACCESS_APPLICATION_RESOURCE_TYPE,
        ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
        ACCESS_POLICY_RESOURCE_TYPE,
    )
    from dokploy_wizard.uninstall.planner import _cloudflare_access_complete

    desired = resolve_desired_state(
        _raw(
            {
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
            }
        )
    )
    provider = {
        ACCESS_OTP_PROVIDER_RESOURCE_TYPE: [
            OwnedResource(
                ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
                "otp-1",
                "account:one:access-otp-provider",
            )
        ]
    }
    def typed(resource_type: str, host: str) -> dict[str, list[OwnedResource]]:
        return {
            **provider,
            resource_type: [
                OwnedResource(resource_type, f"{resource_type}-1", f"account:one:{host}")
            ]
        }

    # Nothing asked for OTP gating, so there is nothing to account for.
    assert _cloudflare_access_complete(
        resolve_desired_state(_raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})),
        {},
    )
    # An OTP policy with no application is not a finished Access configuration, and the
    # LiteLLM admin hostname is an application target on its own.
    assert not _cloudflare_access_complete(desired, provider)
    assert not _cloudflare_access_complete(
        desired, {**provider, **typed(ACCESS_APPLICATION_RESOURCE_TYPE, "access-app:litellm.example.com")}
    )
    assert _cloudflare_access_complete(
        desired,
        {
            **provider,
            ACCESS_APPLICATION_RESOURCE_TYPE: [
                OwnedResource(
                    ACCESS_APPLICATION_RESOURCE_TYPE,
                    "app-1",
                    "account:one:access-app:litellm.example.com",
                )
            ],
            ACCESS_POLICY_RESOURCE_TYPE: [
                OwnedResource(
                    ACCESS_POLICY_RESOURCE_TYPE,
                    "policy-1",
                    "account:one:access-policy:litellm.example.com",
                )
            ],
        },
    )


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


@pytest.mark.parametrize(
    ("existing_overrides", "requested_overrides"),
    [
        (
            {"ENABLE_CODER": "true"},
            {},
        ),
        (
            {"ENABLE_CODER": "true", "ENABLE_SEAWEEDFS": "true"},
            {"ENABLE_SEAWEEDFS": "true"},
        ),
    ],
)
def test_build_pack_disable_plan_caps_checkpoint_before_shared_core_for_litellm_consumer_removal(
    existing_overrides: dict[str, str], requested_overrides: dict[str, str]
) -> None:
    existing_values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "AI_DEFAULT_API_KEY": "shared-key",
        "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
        "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        **existing_overrides,
    }
    requested_values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "AI_DEFAULT_API_KEY": "shared-key",
        "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
        "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        **requested_overrides,
    }
    existing = resolve_desired_state(RawEnvInput(format_version=1, values=existing_values))
    requested = resolve_desired_state(RawEnvInput(format_version=1, values=requested_values))

    plan = build_pack_disable_plan(
        existing_desired=existing,
        requested_desired=requested,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
    )

    assert plan.completed_steps_ceiling == ("preflight", "dokploy_bootstrap", "networking")


def test_build_pack_disable_plan_caps_checkpoint_for_legacy_litellm_consumer_removal() -> None:
    # Insurance: a ledger written by an older wizard can disable openclaw,
    # my-farm-advisor, or surfsense; removing those LiteLLM consumers still has to
    # cap the checkpoint before shared core so the config gets re-rendered.
    requested = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
            },
        )
    )
    # Literal retired pack names: an old box's ledger can disable these packs even
    # though the catalog no longer defines them.
    legacy_packs = ("openclaw", "my-farm-advisor", "surfsense")
    existing = replace(
        requested,
        enabled_packs=tuple(sorted(set(requested.enabled_packs) | set(legacy_packs))),
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


def _access_ledger() -> OwnershipLedger:
    return OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "cloudflare_access_otp_provider", "provider-1", "account:acct:access-otp-provider"
            ),
            OwnedResource(
                "cloudflare_access_application",
                "app-1",
                "account:acct:access-app:litellm.example.com",
            ),
            OwnedResource(
                "cloudflare_access_policy",
                "policy-1",
                "account:acct:access-policy:litellm.example.com",
            ),
        ),
    )


def test_pack_disable_plan_tears_down_access_app_and_policy_when_opted_out() -> None:
    existing = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
            },
        )
    )
    requested = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
        )
    )

    plan = build_pack_disable_plan(
        existing_desired=existing,
        requested_desired=requested,
        ownership_ledger=_access_ledger(),
    )

    deleted = {item.resource.resource_type for item in plan.deletions}
    # Application and policy come down upstream; the provider is Cloudflare's
    # account-level built-in, so only its ledger entry goes away.
    assert deleted == {
        "cloudflare_access_application",
        "cloudflare_access_policy",
        "cloudflare_access_otp_provider",
    }
    assert plan.retained_resources == ()


def test_shell_uninstall_backend_calls_cloudflare_for_access_deletions() -> None:
    from dokploy_wizard.networking import CloudflareAccessApplication
    from dokploy_wizard.uninstall import ShellUninstallBackend
    from dokploy_wizard.uninstall.planner import PlannedDeletion

    class RecordingCloudflare:
        def __init__(self) -> None:
            self.deleted_apps: list[tuple[str, str]] = []
            self.deleted_policies: list[tuple[str, str, str]] = []

        def find_access_application_by_domain(
            self, account_id: str, domain: str
        ) -> CloudflareAccessApplication:
            del account_id
            return CloudflareAccessApplication(
                app_id="app-1",
                name="x",
                domain=domain,
                app_type="self_hosted",
                allowed_identity_provider_ids=("p",),
            )

        def delete_access_application(self, account_id: str, app_id: str) -> None:
            self.deleted_apps.append((account_id, app_id))

        def delete_access_policy(self, account_id: str, app_id: str, policy_id: str) -> None:
            self.deleted_policies.append((account_id, app_id, policy_id))

    backend = RecordingCloudflare()
    deleter = ShellUninstallBackend(
        RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.com"}), cloudflare_backend=backend
    )
    ledger = _access_ledger()
    app = ledger.resources[1]
    policy = ledger.resources[2]

    deleter.delete(PlannedDeletion(resource=policy, phase="cloudflare_access", policy="retain_safe"))
    deleter.delete(PlannedDeletion(resource=app, phase="cloudflare_access", policy="retain_safe"))

    assert backend.deleted_policies == [("acct", "app-1", "policy-1")]
    assert backend.deleted_apps == [("acct", "app-1")]


def test_shell_uninstall_backend_without_cloudflare_stays_ledger_only() -> None:
    from dokploy_wizard.uninstall import ShellUninstallBackend
    from dokploy_wizard.uninstall.planner import PlannedDeletion

    deleter = ShellUninstallBackend(RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.com"}))
    app = _access_ledger().resources[1]
    deleter.delete(PlannedDeletion(resource=app, phase="cloudflare_access", policy="retain_safe"))
