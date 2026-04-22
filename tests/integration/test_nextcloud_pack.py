# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.core import SharedCoreResourceRecord
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployNextcloudBackend,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.nextcloud import NextcloudError, NextcloudResourceRecord
from dokploy_wizard.state import RawEnvInput, load_state_dir, resolve_desired_state

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool
    healthy_after_install: bool
    install_calls: int = 0

    def is_healthy(self) -> bool:
        if self.install_calls == 0:
            return self.healthy_before_install
        return self.healthy_after_install

    def install(self) -> None:
        self.install_calls += 1


@dataclass
class FakeCloudflareBackend:
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, CloudflareDnsRecord] = field(default_factory=dict)
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)

    def validate_account_access(self, account_id: str) -> None:
        del account_id

    def validate_zone_access(self, zone_id: str) -> None:
        del zone_id

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        del account_id
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        del account_id
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        del account_id
        self.existing_tunnel = CloudflareTunnel(tunnel_id="nextcloud-tunnel", name=tunnel_name)
        return self.existing_tunnel

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        del account_id, tunnel_id, ingress

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        del zone_id, record_type
        record = self.dns_records.get(hostname)
        if record is None:
            return ()
        if content is not None and record.content != content:
            return ()
        return (record,)

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        del zone_id
        record = CloudflareDnsRecord(
            record_id=f"dns-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )
        self.dns_records[hostname] = record
        return record

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.provider_id == provider_id:
            return self.access_provider
        return None

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.name == name:
            return self.access_provider
        return None

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        del account_id
        self.access_provider = CloudflareAccessIdentityProvider(
            provider_id="otp-provider-1",
            name=name,
            provider_type="onetimepin",
        )
        return self.access_provider

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return next((item for item in self.access_apps.values() if item.app_id == app_id), None)

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return self.access_apps.get(domain)

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
        del account_id
        app = CloudflareAccessApplication(
            app_id=f"app-{domain}",
            name=name,
            domain=domain,
            app_type="self_hosted",
            allowed_identity_provider_ids=allowed_identity_provider_ids,
        )
        self.access_apps[domain] = app
        return app

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        del account_id, policy_id
        return self.access_policies.get(app_id)

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
        del account_id
        policy = self.access_policies.get(app_id)
        if policy is not None and policy.name == name:
            return policy
        return None

    def create_access_policy(
        self,
        account_id: str,
        *,
        app_id: str,
        name: str,
        emails: tuple[str, ...],
    ) -> CloudflareAccessPolicy:
        del account_id
        policy = CloudflareAccessPolicy(
            policy_id=f"policy-{app_id}",
            app_id=app_id,
            name=name,
            decision="allow",
            emails=emails,
        )
        self.access_policies[app_id] = policy
        return policy


@dataclass
class FakeSharedCoreBackend:
    network: SharedCoreResourceRecord | None = None
    postgres: SharedCoreResourceRecord | None = None
    redis: SharedCoreResourceRecord | None = None

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        self.network = SharedCoreResourceRecord(
            resource_id="network-1", resource_name=resource_name
        )
        return self.network

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.postgres is not None and self.postgres.resource_id == resource_id:
            return self.postgres
        return None

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.postgres is not None and self.postgres.resource_name == resource_name:
            return self.postgres
        return None

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.postgres = SharedCoreResourceRecord(
            resource_id="postgres-1", resource_name=resource_name
        )
        return self.postgres

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.redis is not None and self.redis.resource_id == resource_id:
            return self.redis
        return None

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.redis is not None and self.redis.resource_name == resource_name:
            return self.redis
        return None

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.redis = SharedCoreResourceRecord(resource_id="redis-1", resource_name=resource_name)
        return self.redis

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        del allocations


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        del hostname, secret_refs
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return True


@dataclass
class FakeNextcloudBackend:
    services: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    volumes: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    health: dict[str, bool] = field(default_factory=dict)
    create_service_calls: int = 0
    create_volume_calls: int = 0

    def get_service(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self.services.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_service_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        return self.services.get(resource_name)

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del hostname, data_volume_name, config
        self.create_service_calls += 1
        record = NextcloudResourceRecord(
            resource_id=f"service:{resource_name}",
            resource_name=resource_name,
        )
        self.services[resource_name] = record
        return record

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            data_volume_name=data_volume_name,
            config=config,
        )

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self.volumes.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        return self.volumes.get(resource_name)

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord:
        self.create_volume_calls += 1
        record = NextcloudResourceRecord(
            resource_id=f"volume:{resource_name}",
            resource_name=resource_name,
        )
        self.volumes[resource_name] = record
        return record

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        del url
        return self.health.get(service.resource_name, True)

    def ensure_application_ready(self, *, nextcloud_url: str, onlyoffice_url: str) -> None:
        del nextcloud_url, onlyoffice_url

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        del admin_user


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    schedules: list[DokployScheduleRecord] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(
                    DokployEnvironmentSummary(
                        environment_id="env-1",
                        name="production",
                        is_default=True,
                        composes=(),
                    ),
                ),
            )
        )
        return DokployCreatedProject(project_id="proj-1", environment_id="env-1")

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del compose_file, app_name
        self.create_compose_calls += 1
        record = DokployComposeRecord(compose_id="cmp-1", name=name)
        self.projects[0] = DokployProjectSummary(
            project_id="proj-1",
            name=self.projects[0].name,
            environments=(
                DokployEnvironmentSummary(
                    environment_id=environment_id,
                    name="production",
                    is_default=True,
                    composes=(
                        DokployComposeSummary(
                            compose_id=record.compose_id,
                            name=record.name,
                            status=None,
                        ),
                    ),
                ),
            ),
        )
        return record

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        del compose_id, compose_file
        raise AssertionError("Nextcloud backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        del compose_id
        return tuple(self.schedules)

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        del compose_id
        record = DokployScheduleRecord(
            schedule_id="sch-1",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record

    def update_schedule(
        self,
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        del compose_id
        record = DokployScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record


def _owned_dns_records() -> dict[str, CloudflareDnsRecord]:
    return {
        "dokploy.example.com": CloudflareDnsRecord(
            record_id="dns-dokploy.example.com",
            name="dokploy.example.com",
            record_type="CNAME",
            content="nextcloud-tunnel.cfargotunnel.com",
            proxied=True,
        ),
        "headscale.example.com": CloudflareDnsRecord(
            record_id="dns-headscale.example.com",
            name="headscale.example.com",
            record_type="CNAME",
            content="nextcloud-tunnel.cfargotunnel.com",
            proxied=True,
        ),
        "nextcloud.example.com": CloudflareDnsRecord(
            record_id="dns-nextcloud.example.com",
            name="nextcloud.example.com",
            record_type="CNAME",
            content="nextcloud-tunnel.cfargotunnel.com",
            proxied=True,
        ),
        "office.example.com": CloudflareDnsRecord(
            record_id="dns-office.example.com",
            name="office.example.com",
            record_type="CNAME",
            content="nextcloud-tunnel.cfargotunnel.com",
            proxied=True,
        ),
    }


def test_install_reconciles_nextcloud_pair_and_persists_runtime_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=FakeNextcloudBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["nextcloud"]["outcome"] == "applied"
    assert (
        summary["nextcloud"]["nextcloud"]["service"]["resource_name"] == "nextcloud-stack-nextcloud"
    )
    assert (
        summary["nextcloud"]["onlyoffice"]["service"]["resource_name"]
        == "nextcloud-stack-onlyoffice"
    )
    assert (
        summary["nextcloud"]["nextcloud"]["config"]["onlyoffice_url"]
        == "https://office.example.com"
    )
    assert (
        summary["nextcloud"]["onlyoffice"]["config"]["integration_secret_ref"]
        == "nextcloud-stack-nextcloud-onlyoffice-jwt-secret"
    )
    assert summary["nextcloud"]["nextcloud"]["health_check"]["passed"] is True
    assert summary["nextcloud"]["onlyoffice"]["health_check"]["passed"] is True
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
        "nextcloud",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:headscale.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:nextcloud.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:office.example.com"),
        ("headscale_service", "stack:nextcloud-stack:headscale"),
        ("shared_core_network", "stack:nextcloud-stack:shared-network"),
        ("shared_core_postgres", "stack:nextcloud-stack:shared-postgres"),
        ("shared_core_redis", "stack:nextcloud-stack:shared-redis"),
        ("nextcloud_service", "stack:nextcloud-stack:nextcloud-service"),
        ("onlyoffice_service", "stack:nextcloud-stack:onlyoffice-service"),
        ("nextcloud_volume", "stack:nextcloud-stack:nextcloud-volume"),
        ("onlyoffice_volume", "stack:nextcloud-stack:onlyoffice-volume"),
    }


def test_install_reconciles_nextcloud_pair_via_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._nextcloud_status_ready",
        lambda url: url == "https://nextcloud.example.com/status.php",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._local_https_health_check",
        lambda url: url == "https://office.example.com/healthcheck",
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=DokployNextcloudBackend(
            api_url="https://dokploy.example.com",
            api_key="dokp-key-123",
            stack_name=desired_state.stack_name,
            nextcloud_hostname=desired_state.hostnames["nextcloud"],
            onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
            postgres_service_name=desired_state.shared_core.postgres.service_name,
            redis_service_name=desired_state.shared_core.redis.service_name,
            postgres=allocation.postgres,
            redis=allocation.redis,
            integration_secret_ref="nextcloud-stack-nextcloud-onlyoffice-jwt-secret",
            client=client,
        ),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["nextcloud"]["outcome"] == "applied"
    assert summary["nextcloud"]["nextcloud"]["service"]["resource_id"] == (
        "dokploy-compose:cmp-1:nextcloud-service"
    )
    assert summary["nextcloud"]["onlyoffice"]["service"]["resource_id"] == (
        "dokploy-compose:cmp-1:onlyoffice-service"
    )
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert loaded_state.ownership_ledger is not None


def test_install_rerun_reuses_owned_nextcloud_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    nextcloud_backend = FakeNextcloudBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=nextcloud_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="nextcloud-tunnel", name="nextcloud-stack-tunnel"
            ),
            dns_records=_owned_dns_records(),
        ),
        shared_core_backend=FakeSharedCoreBackend(
            network=SharedCoreResourceRecord(
                resource_id="network-1", resource_name="nextcloud-stack-shared"
            ),
            postgres=SharedCoreResourceRecord(
                resource_id="postgres-1", resource_name="nextcloud-stack-shared-postgres"
            ),
            redis=SharedCoreResourceRecord(
                resource_id="redis-1", resource_name="nextcloud-stack-shared-redis"
            ),
        ),
        headscale_backend=FakeHeadscaleBackend(
            existing_service=HeadscaleResourceRecord(
                resource_id="headscale-service-1",
                resource_name="nextcloud-stack-headscale",
            )
        ),
        nextcloud_backend=nextcloud_backend,
    )

    assert summary["nextcloud"]["outcome"] == "already_present"
    assert summary["nextcloud"]["nextcloud"]["service"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["onlyoffice"]["service"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["nextcloud"]["data_volume"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["onlyoffice"]["data_volume"]["action"] == "reuse_owned"
    assert nextcloud_backend.create_service_calls == 2
    assert nextcloud_backend.create_volume_calls == 2


def test_install_fails_before_nextcloud_checkpoint_when_onlyoffice_health_fails(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"

    with pytest.raises(NextcloudError, match="OnlyOffice health check failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "nextcloud.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(),
            nextcloud_backend=FakeNextcloudBackend(health={"nextcloud-stack-onlyoffice": False}),
        )

    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    assert "nextcloud" not in loaded_state.applied_state.completed_steps
