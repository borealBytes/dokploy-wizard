# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.core import SharedCoreResourceRecord, SharedPostgresAllocation
from dokploy_wizard.core.reconciler import SharedCoreError, reconcile_shared_core
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
    DokploySharedCoreBackend,
)
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.nextcloud import (
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudResourceRecord,
    TalkRuntime,
)
from dokploy_wizard.state import (
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    load_state_dir,
    resolve_desired_state,
)

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
        return None

    def validate_zone_access(self, zone_id: str) -> None:
        return None

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
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
    create_network_calls: int = 0
    create_postgres_calls: int = 0
    create_redis_calls: int = 0
    ensured_allocations: tuple[SharedPostgresAllocation, ...] = ()

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_network_calls += 1
        self.network = SharedCoreResourceRecord(
            resource_id="network-1",
            resource_name=resource_name,
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
        self.create_postgres_calls += 1
        self.postgres = SharedCoreResourceRecord(
            resource_id="postgres-1",
            resource_name=resource_name,
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
        self.create_redis_calls += 1
        self.redis = SharedCoreResourceRecord(
            resource_id="redis-1",
            resource_name=resource_name,
        )
        return self.redis

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        self.ensured_allocations = allocations


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        environment = DokployEnvironmentSummary(
            environment_id="env-1",
            name="production",
            is_default=True,
            composes=(),
        )
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(environment,),
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
            project_id=self.projects[0].project_id,
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
        del compose_file
        self.update_compose_calls += 1
        return DokployComposeRecord(compose_id=compose_id, name="nextcloud-stack-shared")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


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
        record = NextcloudResourceRecord(
            resource_id=f"volume:{resource_name}",
            resource_name=resource_name,
        )
        self.volumes[resource_name] = record
        return record

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        del service, url
        return True

    def ensure_application_ready(
        self, *, nextcloud_url: str, onlyoffice_url: str
    ) -> NextcloudBundleVerification:
        del nextcloud_url, onlyoffice_url
        return NextcloudBundleVerification(
            onlyoffice_document_server_check=NextcloudCommandCheck(
                command="php occ onlyoffice:documentserver --check",
                passed=True,
            ),
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
        )


def test_install_plans_and_persists_shared_core_once_for_nextcloud(tmp_path: Path) -> None:
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

    assert summary["shared_core"]["outcome"] == "applied"
    assert summary["shared_core"]["network"]["resource_name"] == "nextcloud-stack-shared"
    assert summary["shared_core"]["postgres"]["resource_name"] == "nextcloud-stack-shared-postgres"
    assert summary["shared_core"]["redis"]["resource_name"] == "nextcloud-stack-shared-redis"
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


def test_install_rerun_reuses_owned_shared_core_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    shared_backend = FakeSharedCoreBackend()
    headscale_backend = FakeHeadscaleBackend()
    nextcloud_backend = FakeNextcloudBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        nextcloud_backend=nextcloud_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="nextcloud-tunnel",
                name="nextcloud-stack-tunnel",
            ),
            dns_records={
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
            },
        ),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        nextcloud_backend=nextcloud_backend,
    )

    assert summary["shared_core"]["outcome"] == "already_present"
    assert summary["shared_core"]["network"]["action"] == "reuse_owned"
    assert summary["shared_core"]["postgres"]["action"] == "reuse_owned"
    assert summary["shared_core"]["redis"]["action"] == "reuse_owned"
    assert shared_backend.create_network_calls == 1
    assert shared_backend.create_postgres_calls == 1
    assert shared_backend.create_redis_calls == 1
    assert shared_backend.ensured_allocations == (
        SharedPostgresAllocation(
            database_name="nextcloud_stack_nextcloud",
            user_name="nextcloud_stack_nextcloud",
            password_secret_ref="nextcloud-stack-nextcloud-postgres-password",
        ),
    )


def test_install_fails_closed_when_shared_core_owned_resource_drifted(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    headscale_backend = FakeHeadscaleBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=headscale_backend,
        nextcloud_backend=FakeNextcloudBackend(),
    )

    with pytest.raises(SharedCoreError, match="shared-core resource 'shared_core_network' exists"):
        run_install_flow(
            env_file=FIXTURES_DIR / "nextcloud.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(
                existing_tunnel=CloudflareTunnel(
                    tunnel_id="nextcloud-tunnel",
                    name="nextcloud-stack-tunnel",
                ),
                dns_records={
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
                },
            ),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=headscale_backend,
            nextcloud_backend=FakeNextcloudBackend(),
        )


def test_dokploy_shared_core_backend_creates_project_compose_and_reuses_owned_resources() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_API_KEY": "dokp-key-123",
            },
        )
    )
    client = FakeDokployApiClient()
    provisioned: list[tuple[SharedPostgresAllocation, ...]] = []
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        client=client,
        allocation_provisioner=lambda allocations: provisioned.append(allocations),
    )

    phase = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.network is not None
    assert phase.result.postgres is not None
    assert phase.result.redis is not None
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert provisioned == [
        (
            SharedPostgresAllocation(
                database_name="nextcloud_stack_nextcloud",
                user_name="nextcloud_stack_nextcloud",
                password_secret_ref="nextcloud-stack-nextcloud-postgres-password",
            ),
        )
    ]

    reused = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="shared_core_network",
                    resource_id=phase.network_resource_id or "",
                    scope="stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    resource_type="shared_core_postgres",
                    resource_id=phase.postgres_resource_id or "",
                    scope="stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    resource_type="shared_core_redis",
                    resource_id=phase.redis_resource_id or "",
                    scope="stack:nextcloud-stack:shared-redis",
                ),
            ),
        ),
        backend=backend,
    )

    assert reused.result.outcome == "already_present"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.update_compose_calls == 0
    assert client.deploy_calls == 1
