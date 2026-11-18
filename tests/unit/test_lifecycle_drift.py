# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false, reportArgumentType=false, reportOptionalMemberAccess=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dokploy_wizard.lifecycle.drift import LifecycleDriftError, validate_preserved_phases
from dokploy_wizard.networking import CloudflareDnsRecord, CloudflareTunnel
from dokploy_wizard.networking.planner import (
    _build_tunnel_ingress,
    resolve_litellm_admin_hostname,
)
from dokploy_wizard.packs.docuseal.models import (
    DocuSealBootstrapState,
    DocuSealHealthState,
    DocuSealManagedResource,
    DocuSealResult,
)
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudCommandCheck,
    NextcloudHealthCheck,
    NextcloudManagedResource,
    NextcloudPostgresBinding,
    NextcloudRedisBinding,
    NextcloudResult,
    NextcloudServiceConfig,
    NextcloudServiceRuntime,
    OnlyofficeServiceConfig,
    OnlyofficeServiceRuntime,
    TalkRuntime,
)
from dokploy_wizard.packs.seaweedfs.models import (
    SeaweedFsHealthCheck,
    SeaweedFsManagedResource,
    SeaweedFsResult,
)
from dokploy_wizard.state import (
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    ensure_litellm_generated_keys,
    resolve_desired_state,
)

_UNUSED_BACKEND = cast(Any, object())


def _validate(
    *,
    raw_env: RawEnvInput,
    desired_state: Any,
    ownership_ledger: OwnershipLedger,
    preserved_phases: tuple[str, ...],
    bootstrap_backend: Any = _UNUSED_BACKEND,
    tailscale_backend: Any = _UNUSED_BACKEND,
    networking_backend: Any = _UNUSED_BACKEND,
    shared_core_backend: Any = _UNUSED_BACKEND,
    headscale_backend: Any = _UNUSED_BACKEND,
    matrix_backend: Any = _UNUSED_BACKEND,
    nextcloud_backend: Any = _UNUSED_BACKEND,
    seaweedfs_backend: Any = _UNUSED_BACKEND,
    coder_backend: Any = _UNUSED_BACKEND,
    moodle_backend: Any | None = None,
    docuseal_backend: Any | None = None,
) -> Any:
    return validate_preserved_phases(
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        preserved_phases=preserved_phases,
        bootstrap_backend=bootstrap_backend,
        tailscale_backend=tailscale_backend,
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        nextcloud_backend=nextcloud_backend,
        seaweedfs_backend=seaweedfs_backend,
        coder_backend=coder_backend,
        moodle_backend=moodle_backend,
        docuseal_backend=docuseal_backend,
    )


def test_validate_preserved_phases_allows_legacy_cloudflare_access_ownership_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "admin@example.com",
            },
        )
    )
    raw_env = RawEnvInput(
        format_version=1,
        values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"},
    )
    ownership_ledger = OwnershipLedger(format_version=1, resources=())

    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_cloudflare_access",
        lambda **_: SimpleNamespace(
            result=SimpleNamespace(
                outcome="plan_only",
                otp_provider=SimpleNamespace(action="reuse_existing"),
                applications=(SimpleNamespace(action="create"),),
                policies=(SimpleNamespace(action="create"),),
            )
        ),
    )

    report = _validate(
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        preserved_phases=("cloudflare_access",),
    )

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.phase == "cloudflare_access"
    assert entry.status == "ok"
    assert "legacy no-ledger pattern" in entry.detail


def test_validate_preserved_phases_still_rejects_cloudflare_access_drift_when_owned_resources_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "admin@example.com",
            },
        )
    )
    raw_env = RawEnvInput(
        format_version=1,
        values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"},
    )
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                resource_type="cloudflare_access_application",
                resource_id="app-litellm-admin",
                scope="account:account-123:access-app:litellm.openmerge.me",
            ),
        ),
    )

    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_cloudflare_access",
        lambda **_: SimpleNamespace(
            result=SimpleNamespace(
                outcome="plan_only",
                otp_provider=SimpleNamespace(action="reuse_existing"),
                applications=(SimpleNamespace(action="create"),),
                policies=(SimpleNamespace(action="create"),),
            )
        ),
    )

    with pytest.raises(LifecycleDriftError, match="cloudflare_access"):
        _validate(
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            preserved_phases=("cloudflare_access",),
        )


def test_validate_preserved_phases_rejects_unhealthy_preserved_nextcloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    nextcloud_runtime = NextcloudServiceRuntime(
        hostname="nextcloud.openmerge.me",
        url="https://nextcloud.openmerge.me",
        service=NextcloudManagedResource("reuse_owned", "svc-1", "openmerge-nextcloud"),
        data_volume=NextcloudManagedResource("reuse_owned", "vol-1", "openmerge-nextcloud-data"),
        health_check=NextcloudHealthCheck(
            url="https://nextcloud.openmerge.me/status.php", passed=None
        ),
        config=NextcloudServiceConfig(
            onlyoffice_url="https://office.openmerge.me",
            postgres=NextcloudPostgresBinding("db", "user", "secret"),
            redis=NextcloudRedisBinding("redis", "secret"),
        ),
    )
    onlyoffice_runtime = OnlyofficeServiceRuntime(
        hostname="office.openmerge.me",
        url="https://office.openmerge.me",
        service=NextcloudManagedResource("reuse_owned", "svc-2", "openmerge-onlyoffice"),
        data_volume=NextcloudManagedResource("reuse_owned", "vol-2", "openmerge-onlyoffice-data"),
        health_check=NextcloudHealthCheck(
            url="https://office.openmerge.me/healthcheck", passed=None
        ),
        config=OnlyofficeServiceConfig(
            nextcloud_url="https://nextcloud.openmerge.me",
            integration_secret_ref="jwt",
        ),
        document_server_check=NextcloudCommandCheck(
            command="php occ onlyoffice:documentserver --check",
            passed=True,
        ),
    )
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_nextcloud",
        lambda **_: SimpleNamespace(
            result=NextcloudResult(
                outcome="already_present",
                enabled=True,
                nextcloud=nextcloud_runtime,
                onlyoffice=onlyoffice_runtime,
                talk=TalkRuntime(
                    app_id="spreed",
                    enabled=True,
                    enabled_check=NextcloudCommandCheck(
                        command="php occ app:list --output=json",
                        passed=True,
                    ),
                    signaling_check=NextcloudCommandCheck(
                        command="php occ talk:signaling:list --output=json",
                        passed=True,
                    ),
                    stun_check=NextcloudCommandCheck(
                        command="php occ talk:stun:list --output=json",
                        passed=True,
                    ),
                    turn_check=NextcloudCommandCheck(
                        command="php occ talk:turn:list --output=json",
                        passed=True,
                    ),
                ),
                notes=(),
            )
        ),
    )
    nextcloud_backend = SimpleNamespace(check_health=lambda *, service, url: False)

    with pytest.raises(LifecycleDriftError, match="Nextcloud or OnlyOffice is no longer healthy"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("nextcloud",),
            nextcloud_backend=nextcloud_backend,
        )


def test_validate_preserved_phases_rejects_invalid_shared_core_postgres_allocations() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    shared_core_backend = SimpleNamespace(
        get_network=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared"
        ),
        find_network_by_name=lambda resource_name: None,
        create_network=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_postgres_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-postgres"
        ),
        find_postgres_service_by_name=lambda resource_name: None,
        create_postgres_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_redis_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-redis"
        ),
        find_redis_service_by_name=lambda resource_name: None,
        create_redis_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_litellm_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-litellm"
        ),
        find_litellm_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_litellm_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        validate_postgres_allocations=lambda allocations: False,
    )

    with pytest.raises(LifecycleDriftError, match="Shared-core Postgres allocations are not ready"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="shared_core_network",
                        resource_id="net-1",
                        scope="stack:openmerge:shared-network",
                    ),
                    OwnedResource(
                        resource_type="shared_core_postgres",
                        resource_id="pg-1",
                        scope="stack:openmerge:shared-postgres",
                    ),
                    OwnedResource(
                        resource_type="shared_core_redis",
                        resource_id="redis-1",
                        scope="stack:openmerge:shared-redis",
                    ),
                    OwnedResource(
                        resource_type="shared_core_litellm",
                        resource_id="litellm-1",
                        scope="stack:openmerge:shared-litellm",
                    ),
                ),
            ),
            preserved_phases=("shared_core",),
            shared_core_backend=shared_core_backend,
        )


def test_litellm_rerun_preserves_keys_and_ownership(tmp_path: Path) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
            },
        )
    )
    first_keys = ensure_litellm_generated_keys(tmp_path)
    second_keys = ensure_litellm_generated_keys(tmp_path)
    shared_core_backend = SimpleNamespace(
        get_network=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared"
        ),
        find_network_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_network=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_postgres_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-postgres"
        ),
        find_postgres_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_postgres_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_redis_service=lambda resource_id: None,
        find_redis_service_by_name=lambda resource_name: None,
        create_redis_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_mail_relay_service=lambda resource_id: None,
        find_mail_relay_service_by_name=lambda resource_name: None,
        create_mail_relay_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_litellm_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-litellm"
        ),
        find_litellm_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_litellm_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        validate_postgres_allocations=lambda allocations: allocations
        == (desired_state.shared_core.litellm.postgres,),
        validate_litellm_config=lambda *, desired_state: desired_state.shared_core.litellm is not None,
        validate_litellm_virtual_keys=lambda: True,
    )

    assert second_keys == first_keys

    report = _validate(
        raw_env=RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"},
        ),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="shared_core_network",
                    resource_id="net-1",
                    scope="stack:openmerge:shared-network",
                ),
                OwnedResource(
                    resource_type="shared_core_postgres",
                    resource_id="pg-1",
                    scope="stack:openmerge:shared-postgres",
                ),
                OwnedResource(
                    resource_type="shared_core_litellm",
                    resource_id="litellm-1",
                    scope="stack:openmerge:shared-litellm",
                ),
            ),
        ),
        preserved_phases=("shared_core",),
        shared_core_backend=shared_core_backend,
    )

    assert len(report.entries) == 1
    assert report.entries[0].phase == "shared_core"
    assert report.entries[0].status == "ok"


def test_validate_preserved_phases_rejects_stale_litellm_shared_core_config() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_CODER": "true",
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            },
        )
    )
    shared_core_backend = SimpleNamespace(
        get_network=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared"
        ),
        find_network_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_network=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_postgres_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-postgres"
        ),
        find_postgres_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_postgres_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_redis_service=lambda resource_id: None,
        find_redis_service_by_name=lambda resource_name: None,
        create_redis_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_mail_relay_service=lambda resource_id: None,
        find_mail_relay_service_by_name=lambda resource_name: None,
        create_mail_relay_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_litellm_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-litellm"
        ),
        find_litellm_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_litellm_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        validate_postgres_allocations=lambda allocations: True,
        validate_litellm_config=lambda **_: False,
    )

    with pytest.raises(LifecycleDriftError, match="LiteLLM config no longer matches"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "openmerge",
                    "ROOT_DOMAIN": "openmerge.me",
                    "ENABLE_CODER": "true",
                    "AI_DEFAULT_API_KEY": "shared-key",
                    "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                },
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="shared_core_network",
                        resource_id="net-1",
                        scope="stack:openmerge:shared-network",
                    ),
                    OwnedResource(
                        resource_type="shared_core_postgres",
                        resource_id="pg-1",
                        scope="stack:openmerge:shared-postgres",
                    ),
                    OwnedResource(
                        resource_type="shared_core_litellm",
                        resource_id="litellm-1",
                        scope="stack:openmerge:shared-litellm",
                    ),
                ),
            ),
            preserved_phases=("shared_core",),
            shared_core_backend=shared_core_backend,
        )


def test_validate_preserved_phases_rejects_stale_litellm_virtual_keys() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_CODER": "true",
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            },
        )
    )
    shared_core_backend = SimpleNamespace(
        get_network=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared"
        ),
        find_network_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_network=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_postgres_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-postgres"
        ),
        find_postgres_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_postgres_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_redis_service=lambda resource_id: None,
        find_redis_service_by_name=lambda resource_name: None,
        create_redis_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_mail_relay_service=lambda resource_id: None,
        find_mail_relay_service_by_name=lambda resource_name: None,
        create_mail_relay_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        get_litellm_service=lambda resource_id: SimpleNamespace(
            resource_id=resource_id, resource_name="openmerge-shared-litellm"
        ),
        find_litellm_service_by_name=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        create_litellm_service=lambda resource_name: SimpleNamespace(
            resource_id=resource_name, resource_name=resource_name
        ),
        validate_postgres_allocations=lambda allocations: True,
        validate_litellm_config=lambda **_: True,
        validate_litellm_virtual_keys=lambda: False,
    )

    with pytest.raises(LifecycleDriftError, match="LiteLLM virtual keys no longer match"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "openmerge",
                    "ROOT_DOMAIN": "openmerge.me",
                    "ENABLE_CODER": "true",
                    "AI_DEFAULT_API_KEY": "shared-key",
                    "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                },
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="shared_core_network",
                        resource_id="net-1",
                        scope="stack:openmerge:shared-network",
                    ),
                    OwnedResource(
                        resource_type="shared_core_postgres",
                        resource_id="pg-1",
                        scope="stack:openmerge:shared-postgres",
                    ),
                    OwnedResource(
                        resource_type="shared_core_litellm",
                        resource_id="litellm-1",
                        scope="stack:openmerge:shared-litellm",
                    ),
                ),
            ),
            preserved_phases=("shared_core",),
            shared_core_backend=shared_core_backend,
        )


def test_validate_preserved_phases_rejects_stale_tunnel_ingress() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "ENABLE_HEADSCALE": "true",
            },
        )
    )
    networking_backend = SimpleNamespace(
        get_tunnel_configuration=lambda account_id, tunnel_id: (
            {
                "hostname": "dokploy.openmerge.me",
                "service": "http://localhost:3000",
                "originRequest": {},
            },
            {"service": "http_status:404"},
        ),
        validate_account_access=lambda account_id: None,
        validate_zone_access=lambda zone_id: None,
        get_tunnel=lambda account_id, tunnel_id: CloudflareTunnel(
            tunnel_id=tunnel_id, name="openmerge-tunnel"
        ),
        find_tunnel_by_name=lambda account_id, tunnel_name: CloudflareTunnel(
            tunnel_id="tunnel-123", name=tunnel_name
        ),
        create_tunnel=lambda account_id, tunnel_name: CloudflareTunnel(
            tunnel_id="tunnel-123", name=tunnel_name
        ),
        list_dns_records=lambda zone_id, **kwargs: (
            CloudflareDnsRecord(
                record_id=(
                "dns-1"
                if kwargs["hostname"] == "dokploy.openmerge.me"
                else "dns-3"
                if kwargs["hostname"] == "litellm.openmerge.me"
                else "dns-2"
            ),
                name=kwargs["hostname"],
                record_type="CNAME",
                content=kwargs.get("content")
                or "8df8d550-b8ad-4444-ad28-b17f1291b5f2.cfargotunnel.com",
                proxied=True,
            ),
        ),
        create_dns_record=lambda zone_id, **kwargs: CloudflareDnsRecord(
            record_id="dns-1",
            name=kwargs["hostname"],
            record_type="CNAME",
            content=kwargs["content"],
            proxied=kwargs["proxied"],
        ),
        get_tunnel_token=lambda account_id, tunnel_id: "token",
        update_tunnel_configuration=lambda account_id, tunnel_id, ingress: None,
    )

    with pytest.raises(LifecycleDriftError, match="Cloudflare tunnel ingress no longer matches"):
        _validate(
            raw_env=_networking_drift_raw_env(),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="cloudflare_tunnel",
                        resource_id="tunnel-123",
                        scope="account:account-123",
                    ),
                    OwnedResource(
                        resource_type="cloudflare_dns_record",
                        resource_id="dns-1",
                        scope="zone:zone-123:dokploy.openmerge.me",
                    ),
                    OwnedResource(
                        resource_type="cloudflare_dns_record",
                        resource_id="dns-2",
                        scope="zone:zone-123:headscale.openmerge.me",
                    ),
                    OwnedResource(
                        resource_type="cloudflare_dns_record",
                        resource_id="dns-3",
                        scope="zone:zone-123:litellm.openmerge.me",
                    ),
                ),
            ),
            preserved_phases=("networking",),
            networking_backend=networking_backend,
        )


def _networking_drift_raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
            "CLOUDFLARE_ZONE_ID": "zone-123",
            "ENABLE_HEADSCALE": "true",
        },
    )


def test_validate_preserved_phases_accepts_pushed_tunnel_ingress_including_admin_route() -> None:
    """The drift probe must compare against exactly what the planner pushes.

    The planner's ingress route set includes the on-demand LiteLLM admin
    hostname; an expectation built without it can never match on a
    LiteLLM-backed box, so every noop-classified modify would flag preserved
    drift at 'networking' and rerun the whole downstream chain forever
    (measured live on the box before the probe was aligned).
    """
    raw_env = _networking_drift_raw_env()
    desired_state = resolve_desired_state(raw_env)
    pushed_ingress = _build_tunnel_ingress(
        desired_state,
        litellm_admin_hostname=resolve_litellm_admin_hostname(
            raw_env=raw_env, desired_state=desired_state
        ),
    )
    # Guard the premise: this env really does resolve the admin route.
    assert any(
        rule.get("hostname") == "litellm.openmerge.me" for rule in pushed_ingress
    )

    networking_backend = SimpleNamespace(
        get_tunnel_configuration=lambda account_id, tunnel_id: pushed_ingress,
        validate_account_access=lambda account_id: None,
        validate_zone_access=lambda zone_id: None,
        get_tunnel=lambda account_id, tunnel_id: CloudflareTunnel(
            tunnel_id=tunnel_id, name="openmerge-tunnel"
        ),
        find_tunnel_by_name=lambda account_id, tunnel_name: CloudflareTunnel(
            tunnel_id="tunnel-123", name=tunnel_name
        ),
        create_tunnel=lambda account_id, tunnel_name: CloudflareTunnel(
            tunnel_id="tunnel-123", name=tunnel_name
        ),
        list_dns_records=lambda zone_id, **kwargs: (
            CloudflareDnsRecord(
                record_id=(
                    "dns-1"
                    if kwargs["hostname"] == "dokploy.openmerge.me"
                    else "dns-3"
                    if kwargs["hostname"] == "litellm.openmerge.me"
                    else "dns-2"
                ),
                name=kwargs["hostname"],
                record_type="CNAME",
                content=kwargs.get("content")
                or "8df8d550-b8ad-4444-ad28-b17f1291b5f2.cfargotunnel.com",
                proxied=True,
            ),
        ),
        create_dns_record=lambda zone_id, **kwargs: CloudflareDnsRecord(
            record_id="dns-1",
            name=kwargs["hostname"],
            record_type="CNAME",
            content=kwargs["content"],
            proxied=kwargs["proxied"],
        ),
        get_tunnel_token=lambda account_id, tunnel_id: "token",
        update_tunnel_configuration=lambda account_id, tunnel_id, ingress: None,
    )

    _validate(
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="cloudflare_tunnel",
                    resource_id="tunnel-123",
                    scope="account:account-123",
                ),
                OwnedResource(
                    resource_type="cloudflare_dns_record",
                    resource_id="dns-1",
                    scope="zone:zone-123:dokploy.openmerge.me",
                ),
                OwnedResource(
                    resource_type="cloudflare_dns_record",
                    resource_id="dns-2",
                    scope="zone:zone-123:headscale.openmerge.me",
                ),
                OwnedResource(
                    resource_type="cloudflare_dns_record",
                    resource_id="dns-3",
                    scope="zone:zone-123:litellm.openmerge.me",
                ),
            ),
        ),
        preserved_phases=("networking",),
        networking_backend=networking_backend,
    )


def _docuseal_plan_only_result(initialized: bool | None) -> SimpleNamespace:
    return SimpleNamespace(
        result=DocuSealResult(
            outcome="plan_only",
            enabled=True,
            hostname="docuseal.openmerge.me",
            service=DocuSealManagedResource("reuse_owned", "svc-docuseal", "openmerge-docuseal"),
            persistent_data=DocuSealManagedResource(
                "reuse_owned", "vol-docuseal", "openmerge-docuseal-data"
            ),
            bootstrap_state=DocuSealBootstrapState(
                initialized=initialized,
                secret_key_base_secret_ref="openmerge-docuseal-secret-key-base",
            ),
            health_state=DocuSealHealthState(
                url="https://docuseal.openmerge.me/up", path="/up", passed=None
            ),
            config=None,
            notes=(),
        )
    )


def _docuseal_drift_raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "ENABLE_DOCUSEAL": "true",
        },
    )


def test_validate_preserved_phases_accepts_docuseal_dry_run_unknown_bootstrap_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dry-run lane reports ``initialized=None`` (unknown), which is not drift.

    The probe compared the field truthily, so the unknown read as 'not
    initialized' and every noop-classified modify resumed from 'docuseal'
    forever (third masked probe bug measured on the box, after the tunnel
    ingress and virtual-key ones). An explicit False still drifts.
    """
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_docuseal",
        lambda **_: _docuseal_plan_only_result(None),
    )
    raw_env = _docuseal_drift_raw_env()
    docuseal_backend = SimpleNamespace(check_health=lambda *, service, url: True)

    _validate(
        raw_env=raw_env,
        desired_state=resolve_desired_state(raw_env),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        preserved_phases=("docuseal",),
        docuseal_backend=docuseal_backend,
    )


def test_validate_preserved_phases_rejects_docuseal_bootstrap_state_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_docuseal",
        lambda **_: _docuseal_plan_only_result(False),
    )
    raw_env = _docuseal_drift_raw_env()
    docuseal_backend = SimpleNamespace(check_health=lambda *, service, url: True)

    with pytest.raises(LifecycleDriftError, match="bootstrap state is not initialized"):
        _validate(
            raw_env=raw_env,
            desired_state=resolve_desired_state(raw_env),
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("docuseal",),
            docuseal_backend=docuseal_backend,
        )


def test_validate_preserved_phases_rejects_unhealthy_preserved_seaweedfs_and_coder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "key",
                "SEAWEEDFS_SECRET_KEY": "secret",
                "ENABLE_CODER": "true",
            },
        )
    )
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_seaweedfs",
        lambda **_: SimpleNamespace(
            result=SeaweedFsResult(
                outcome="already_present",
                enabled=True,
                hostname="s3.openmerge.me",
                service=SeaweedFsManagedResource("reuse_owned", "svc-sea", "openmerge-seaweedfs"),
                persistent_data=SeaweedFsManagedResource(
                    "reuse_owned", "vol-sea", "openmerge-seaweedfs-data"
                ),
                access_key="key",
                health_check=SeaweedFsHealthCheck(
                    url="https://s3.openmerge.me/status", passed=None
                ),
                notes=(),
            )
        ),
    )
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_coder",
        lambda **_: SimpleNamespace(
            result=SimpleNamespace(
                outcome="already_present",
                service=SimpleNamespace(
                    action="reuse_owned",
                    resource_id="svc-coder",
                    resource_name="openmerge-coder",
                ),
                persistent_data=SimpleNamespace(
                    action="reuse_owned",
                    resource_id="vol-coder",
                    resource_name="openmerge-coder-data",
                ),
                health_check=SimpleNamespace(url="https://coder.openmerge.me/healthz"),
            )
        ),
    )
    sea_backend = SimpleNamespace(check_health=lambda *, service, url: False)
    coder_backend = SimpleNamespace(check_health=lambda *, service, url: False)

    with pytest.raises(LifecycleDriftError, match="SeaweedFS health check no longer passes"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("seaweedfs",),
            seaweedfs_backend=sea_backend,
        )

    with pytest.raises(LifecycleDriftError, match="Coder health check no longer passes"):
        _validate(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("coder",),
            coder_backend=coder_backend,
        )
