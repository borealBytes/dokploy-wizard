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
    DokployMatrixBackend,
    DokployProjectSummary,
)
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.matrix import MatrixError, MatrixResourceRecord
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
        self.existing_tunnel = CloudflareTunnel(tunnel_id="matrix-tunnel", name=tunnel_name)
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
    create_network_calls: int = 0
    create_postgres_calls: int = 0
    create_redis_calls: int = 0

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

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        raise AssertionError(f"Matrix should not provision mail relay: {resource_name}")


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
class FakeMatrixBackend:
    existing_service: MatrixResourceRecord | None = None
    existing_data: MatrixResourceRecord | None = None
    health_ok: bool = True
    create_service_calls: int = 0
    create_data_calls: int = 0

    def get_service(self, resource_id: str) -> MatrixResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
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
        shared_allocation: object,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> MatrixResourceRecord:
        del (
            hostname,
            secret_refs,
            shared_allocation,
            postgres_service_name,
            redis_service_name,
            data_resource_name,
        )
        self.create_service_calls += 1
        self.existing_service = MatrixResourceRecord(
            resource_id="matrix-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def get_persistent_data(self, resource_id: str) -> MatrixResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> MatrixResourceRecord:
        self.create_data_calls += 1
        self.existing_data = MatrixResourceRecord(
            resource_id="matrix-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool:
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
        raise AssertionError("Matrix backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_install_reconciles_matrix_and_persists_runtime_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["shared_core"]["outcome"] == "applied"
    assert summary["headscale"]["outcome"] == "applied"
    assert summary["matrix"]["outcome"] == "applied"
    assert summary["matrix"]["hostname"] == "matrix.example.com"
    assert summary["matrix"]["service"]["resource_name"] == "matrix-stack-matrix"
    assert summary["matrix"]["persistent_data"]["resource_name"] == "matrix-stack-matrix-data"
    assert summary["matrix"]["shared_postgres_service"] == "matrix-stack-shared-postgres"
    assert summary["matrix"]["shared_redis_service"] == "matrix-stack-shared-redis"
    assert summary["matrix"]["shared_allocation"]["pack_name"] == "matrix"
    assert summary["matrix"]["health_check"]["passed"] is True
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
        "matrix",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:headscale.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:matrix.example.com"),
        ("shared_core_network", "stack:matrix-stack:shared-network"),
        ("shared_core_postgres", "stack:matrix-stack:shared-postgres"),
        ("shared_core_redis", "stack:matrix-stack:shared-redis"),
        ("headscale_service", "stack:matrix-stack:headscale"),
        ("matrix_service", "stack:matrix-stack:matrix-service"),
        ("matrix_data", "stack:matrix-stack:matrix-data"),
    }


def test_install_reconciles_matrix_via_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "matrix-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "matrix"
    )
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.matrix._http_health_check",
        lambda url: url == "https://matrix.example.com/_matrix/client/versions",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.matrix._docker_container_is_up",
        lambda service_name: service_name == "matrix-stack-matrix",
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=DokployMatrixBackend(
            api_url="https://dokploy.example.com",
            api_key="dokp-key-123",
            stack_name=desired_state.stack_name,
            hostname=desired_state.hostnames["matrix"],
            shared_allocation=allocation,
            postgres_service_name=desired_state.shared_core.postgres.service_name,
            redis_service_name=desired_state.shared_core.redis.service_name,
            secret_refs=(
                "matrix-stack-matrix-registration-shared-secret",
                "matrix-stack-matrix-macaroon-secret-key",
            ),
            client=client,
        ),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["matrix"]["outcome"] == "applied"
    assert summary["matrix"]["service"]["resource_id"] == "dokploy-compose:cmp-1:matrix-service"
    assert summary["matrix"]["persistent_data"]["resource_id"] == (
        "dokploy-compose:cmp-1:matrix-data"
    )
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert loaded_state.ownership_ledger is not None


def test_install_rerun_reuses_owned_matrix_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    shared_backend = FakeSharedCoreBackend()
    headscale_backend = FakeHeadscaleBackend()
    matrix_backend = FakeMatrixBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(tunnel_id="matrix-tunnel", name="matrix-stack-tunnel"),
            dns_records={
                "dokploy.example.com": CloudflareDnsRecord(
                    record_id="dns-dokploy.example.com",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="matrix-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "headscale.example.com": CloudflareDnsRecord(
                    record_id="dns-headscale.example.com",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="matrix-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "matrix.example.com": CloudflareDnsRecord(
                    record_id="dns-matrix.example.com",
                    name="matrix.example.com",
                    record_type="CNAME",
                    content="matrix-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            },
        ),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
    )

    assert summary["shared_core"]["outcome"] == "already_present"
    assert summary["headscale"]["outcome"] == "already_present"
    assert summary["matrix"]["outcome"] == "already_present"
    assert summary["matrix"]["service"]["action"] == "reuse_owned"
    assert summary["matrix"]["persistent_data"]["action"] == "reuse_owned"
    assert matrix_backend.create_service_calls == 1
    assert matrix_backend.create_data_calls == 1


def test_install_fails_when_matrix_health_check_does_not_pass(tmp_path: Path) -> None:
    with pytest.raises(MatrixError, match="health check failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "matrix.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(),
            matrix_backend=FakeMatrixBackend(health_ok=False),
        )
