# pyright: reportMissingImports=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from dokploy_wizard.lifecycle.drift import LifecycleDriftError, validate_preserved_phases
from dokploy_wizard.networking import CloudflareDnsRecord, CloudflareTunnel
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudHealthCheck,
    NextcloudManagedResource,
    NextcloudPostgresBinding,
    NextcloudRedisBinding,
    NextcloudResult,
    NextcloudServiceConfig,
    NextcloudServiceRuntime,
    OnlyofficeServiceConfig,
    OnlyofficeServiceRuntime,
)
from dokploy_wizard.packs.openclaw.models import (
    OpenClawHealthCheck,
    OpenClawManagedResource,
    OpenClawResult,
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
    resolve_desired_state,
)

_UNUSED_BACKEND = cast(Any, object())


def test_validate_preserved_phases_allows_legacy_cloudflare_access_ownership_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_OPENCLAW": "true",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "clayton@superiorbyteworks.com",
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

    report = validate_preserved_phases(
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        preserved_phases=("cloudflare_access",),
        bootstrap_backend=_UNUSED_BACKEND,
        tailscale_backend=_UNUSED_BACKEND,
        networking_backend=_UNUSED_BACKEND,
        shared_core_backend=_UNUSED_BACKEND,
        headscale_backend=_UNUSED_BACKEND,
        matrix_backend=_UNUSED_BACKEND,
        nextcloud_backend=_UNUSED_BACKEND,
        seaweedfs_backend=_UNUSED_BACKEND,
        openclaw_backend=_UNUSED_BACKEND,
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
                "ENABLE_OPENCLAW": "true",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "clayton@superiorbyteworks.com",
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
                resource_id="app-openclaw",
                scope="account:account-123:access-app:openclaw.openmerge.me",
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
        validate_preserved_phases(
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            preserved_phases=("cloudflare_access",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=_UNUSED_BACKEND,
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
    )
    monkeypatch.setattr(
        "dokploy_wizard.lifecycle.drift.reconcile_nextcloud",
        lambda **_: SimpleNamespace(
            result=NextcloudResult(
                outcome="already_present",
                enabled=True,
                nextcloud=nextcloud_runtime,
                onlyoffice=onlyoffice_runtime,
                notes=(),
            )
        ),
    )
    nextcloud_backend = SimpleNamespace(check_health=lambda *, service, url: False)

    with pytest.raises(LifecycleDriftError, match="Nextcloud or OnlyOffice is no longer healthy"):
        validate_preserved_phases(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("nextcloud",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=nextcloud_backend,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=_UNUSED_BACKEND,
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
        validate_postgres_allocations=lambda allocations: False,
    )

    with pytest.raises(LifecycleDriftError, match="Shared-core Postgres allocations are not ready"):
        validate_preserved_phases(
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
                ),
            ),
            preserved_phases=("shared_core",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=shared_core_backend,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=_UNUSED_BACKEND,
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
                record_id="dns-1" if kwargs["hostname"] == "dokploy.openmerge.me" else "dns-2",
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
        validate_preserved_phases(
            raw_env=RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "openmerge",
                    "ROOT_DOMAIN": "openmerge.me",
                    "CLOUDFLARE_ACCOUNT_ID": "account-123",
                    "CLOUDFLARE_ZONE_ID": "zone-123",
                    "ENABLE_HEADSCALE": "true",
                },
            ),
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
                ),
            ),
            preserved_phases=("networking",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=networking_backend,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=_UNUSED_BACKEND,
        )


def test_validate_preserved_phases_rejects_unhealthy_preserved_seaweedfs_and_openclaw(
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
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
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
        "dokploy_wizard.lifecycle.drift.reconcile_openclaw",
        lambda **_: SimpleNamespace(
            result=OpenClawResult(
                outcome="already_present",
                enabled=True,
                variant="openclaw",
                hostname="openclaw.openmerge.me",
                channels=("telegram",),
                replicas=1,
                template_path="template",
                service=OpenClawManagedResource("reuse_owned", "svc-openclaw", "openmerge-advisor"),
                secret_refs=(),
                health_check=OpenClawHealthCheck(
                    url="https://openclaw.openmerge.me/health", passed=None
                ),
                notes=(),
            )
        ),
    )
    sea_backend = SimpleNamespace(check_health=lambda *, service, url: False)
    openclaw_backend = SimpleNamespace(check_health=lambda *, service, url: False)

    with pytest.raises(LifecycleDriftError, match="SeaweedFS health check no longer passes"):
        validate_preserved_phases(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("seaweedfs",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=sea_backend,
            openclaw_backend=_UNUSED_BACKEND,
        )

    with pytest.raises(LifecycleDriftError, match="OpenClaw health check no longer passes"):
        validate_preserved_phases(
            raw_env=RawEnvInput(
                format_version=1, values={"STACK_NAME": "openmerge", "ROOT_DOMAIN": "openmerge.me"}
            ),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            preserved_phases=("openclaw",),
            bootstrap_backend=_UNUSED_BACKEND,
            tailscale_backend=_UNUSED_BACKEND,
            networking_backend=_UNUSED_BACKEND,
            shared_core_backend=_UNUSED_BACKEND,
            headscale_backend=_UNUSED_BACKEND,
            matrix_backend=_UNUSED_BACKEND,
            nextcloud_backend=_UNUSED_BACKEND,
            seaweedfs_backend=_UNUSED_BACKEND,
            openclaw_backend=openclaw_backend,
        )
