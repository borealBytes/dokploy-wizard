# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.core import SharedCoreResourceRecord
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployHeadscaleBackend,
    DokployProjectSummary,
)
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleError, HeadscaleResourceRecord
from dokploy_wizard.state import RawEnvInput, load_state_dir

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
        self.existing_tunnel = CloudflareTunnel(tunnel_id="headscale-tunnel", name=tunnel_name)
        return self.existing_tunnel

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
            resource_id="shared-network-1",
            resource_name=resource_name,
        )
        return self.network

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        raise AssertionError(f"Headscale should not provision postgres: {resource_name}")

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        raise AssertionError(f"Headscale should not provision redis: {resource_name}")


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None
    health_ok: bool = True
    create_calls: int = 0

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
        self.create_calls += 1
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
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
        raise AssertionError("Headscale backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_install_reconciles_headscale_and_persists_runtime_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["shared_core"]["outcome"] == "not_required"
    assert summary["headscale"]["outcome"] == "applied"
    assert summary["headscale"]["hostname"] == "headscale.example.com"
    assert summary["headscale"]["service"]["resource_name"] == "headscale-stack-headscale"
    assert summary["headscale"]["health_check"]["passed"] is True
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:headscale.example.com"),
        ("headscale_service", "stack:headscale-stack:headscale"),
    }


def test_install_reconciles_headscale_via_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    client = FakeDokployApiClient()
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale._http_health_check",
        lambda url: url == "https://headscale.example.com/health",
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=DokployHeadscaleBackend(
            api_url="https://dokploy.example.com",
            api_key="dokp-key-123",
            stack_name="headscale-stack",
            hostname="headscale.example.com",
            client=client,
        ),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["headscale"]["outcome"] == "applied"
    assert summary["headscale"]["service"]["resource_id"] == "dokploy-compose:cmp-1:headscale"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert loaded_state.ownership_ledger is not None
    assert (
        "headscale_service",
        "stack:headscale-stack:headscale",
    ) in {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    }


def test_install_rerun_reuses_owned_headscale_service(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    headscale_backend = FakeHeadscaleBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=headscale_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="headscale-tunnel",
                name="headscale-stack-tunnel",
            ),
            dns_records={
                "dokploy.example.com": CloudflareDnsRecord(
                    record_id="dns-dokploy.example.com",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="headscale-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "headscale.example.com": CloudflareDnsRecord(
                    record_id="dns-headscale.example.com",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="headscale-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            },
        ),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=headscale_backend,
    )

    assert summary["headscale"]["outcome"] == "already_present"
    assert summary["headscale"]["service"]["action"] == "reuse_owned"
    assert headscale_backend.create_calls == 1


def test_install_skips_headscale_when_explicitly_disabled(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "headscale-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "false",
                "HOST_OS_ID": "ubuntu",
                "HOST_OS_VERSION_ID": "24.04",
                "HOST_CPU_COUNT": "2",
                "HOST_MEMORY_GB": "4",
                "HOST_DISK_GB": "40",
                "HOST_DOCKER_INSTALLED": "true",
                "HOST_DOCKER_DAEMON_REACHABLE": "true",
                "HOST_PORT_80_IN_USE": "false",
                "HOST_PORT_443_IN_USE": "false",
                "HOST_PORT_3000_IN_USE": "false",
                "HOST_ENVIRONMENT": "local",
                "DOKPLOY_BOOTSTRAP_HEALTHY": "true",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "CLOUDFLARE_TUNNEL_NAME": "headscale-stack-tunnel",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["headscale"]["outcome"] == "skipped"
    assert loaded_state.ownership_ledger is not None
    assert all(
        resource.resource_type != "headscale_service"
        for resource in loaded_state.ownership_ledger.resources
    )


def test_install_fails_when_headscale_health_check_does_not_pass(tmp_path: Path) -> None:
    with pytest.raises(HeadscaleError, match="health check failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "headscale.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(health_ok=False),
        )
