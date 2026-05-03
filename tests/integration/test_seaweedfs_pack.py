# pyright: reportMissingImports=false
# mypy: disable-error-code=no-untyped-def

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
    DokployProjectSummary,
    DokploySeaweedFsBackend,
)
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.state import RawEnvInput, load_state_dir, resolve_desired_state

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool
    healthy_after_install: bool
    install_calls: int = 0

    def is_healthy(self) -> bool:
        return (
            self.healthy_before_install if self.install_calls == 0 else self.healthy_after_install
        )

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
        self.existing_tunnel = CloudflareTunnel(tunnel_id="seaweed-tunnel", name=tunnel_name)
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

    def get_access_identity_provider(self, account_id: str, provider_id: str):
        del account_id, provider_id
        return None

    def find_access_identity_provider_by_name(self, account_id: str, name: str):
        del account_id, name
        return None

    def create_access_identity_provider(self, account_id: str, name: str):
        raise AssertionError("SeaweedFS should not create Cloudflare Access resources")

    def get_access_application(self, account_id: str, app_id: str):
        del account_id, app_id
        return None

    def find_access_application_by_domain(self, account_id: str, domain: str):
        del account_id, domain
        return None

    def create_access_application(
        self, account_id: str, *, name: str, domain: str, allowed_identity_provider_ids
    ):
        raise AssertionError("SeaweedFS should not create Cloudflare Access apps")

    def get_access_policy(self, account_id: str, app_id: str, policy_id: str):
        del account_id, app_id, policy_id
        return None

    def find_access_policy_by_name(self, account_id: str, app_id: str, name: str):
        del account_id, app_id, name
        return None

    def create_access_policy(self, account_id: str, *, app_id: str, name: str, emails):
        raise AssertionError("SeaweedFS should not create Cloudflare Access policies")


@dataclass
class FakeSharedCoreBackend:
    network: SharedCoreResourceRecord | None = None
    postgres: SharedCoreResourceRecord | None = None
    redis: SharedCoreResourceRecord | None = None
    litellm: SharedCoreResourceRecord | None = None

    def get_network(self, resource_id: str):
        if self.network is not None and self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str):
        if self.network is not None and self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str):
        self.network = SharedCoreResourceRecord(
            resource_id="network-1", resource_name=resource_name
        )
        return self.network

    def get_postgres_service(self, resource_id: str):
        if self.postgres is not None and self.postgres.resource_id == resource_id:
            return self.postgres
        return None

    def find_postgres_service_by_name(self, resource_name: str):
        if self.postgres is not None and self.postgres.resource_name == resource_name:
            return self.postgres
        return None

    def create_postgres_service(self, resource_name: str):
        self.postgres = SharedCoreResourceRecord(
            resource_id="postgres-1", resource_name=resource_name
        )
        return self.postgres

    def get_redis_service(self, resource_id: str):
        if self.redis is not None and self.redis.resource_id == resource_id:
            return self.redis
        return None

    def find_redis_service_by_name(self, resource_name: str):
        if self.redis is not None and self.redis.resource_name == resource_name:
            return self.redis
        return None

    def create_redis_service(self, resource_name: str):
        self.redis = SharedCoreResourceRecord(resource_id="redis-1", resource_name=resource_name)
        return self.redis

    def get_mail_relay_service(self, resource_id: str):
        del resource_id
        return None

    def find_mail_relay_service_by_name(self, resource_name: str):
        del resource_name
        return None

    def create_mail_relay_service(self, resource_name: str):
        raise AssertionError(f"SeaweedFS should not provision mail relay: {resource_name}")

    def get_litellm_service(self, resource_id: str):
        if self.litellm is not None and self.litellm.resource_id == resource_id:
            return self.litellm
        return None

    def find_litellm_service_by_name(self, resource_name: str):
        if self.litellm is not None and self.litellm.resource_name == resource_name:
            return self.litellm
        return None

    def create_litellm_service(self, resource_name: str):
        self.litellm = SharedCoreResourceRecord(
            resource_id="litellm-1", resource_name=resource_name
        )
        return self.litellm


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str):
        return None

    def find_service_by_name(self, resource_name: str):
        return None

    def create_service(self, *, resource_name: str, hostname: str, secret_refs: tuple[str, ...]):
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
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(self, *, name: str, description: str | None, env: str | None):
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

    def create_compose(self, *, name: str, environment_id: str, compose_file: str, app_name: str):
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

    def update_compose(self, *, compose_id: str, compose_file: str):
        del compose_id, compose_file
        raise AssertionError("SeaweedFS backend should not update compose apps in this task")

    def deploy_compose(self, *, compose_id: str, title: str | None, description: str | None):
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_install_reconciles_seaweedfs_pack_via_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "seaweedfs-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            },
        )
    )
    client = FakeDokployApiClient()
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.seaweedfs._local_https_health_check",
        lambda url: url == "https://s3.example.com/status",
    )

    summary = run_install_flow(
        env_file=tmp_path / "seaweedfs.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "seaweedfs-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_API_KEY": "dokp-key-123",
                "HOST_OS_ID": "ubuntu",
                "HOST_OS_VERSION_ID": "24.04",
                "HOST_CPU_COUNT": "4",
                "HOST_MEMORY_GB": "8",
                "HOST_DISK_GB": "100",
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
                "CLOUDFLARE_MOCK_ACCOUNT_OK": "true",
                "CLOUDFLARE_MOCK_ZONE_OK": "true",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        seaweedfs_backend=DokploySeaweedFsBackend(
            api_url="https://dokploy.example.com",
            api_key="dokp-key-123",
            stack_name=desired_state.stack_name,
            hostname=desired_state.hostnames["s3"],
            access_key="seaweed-access",
            secret_key="seaweed-secret",
            client=client,
        ),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["seaweedfs"]["outcome"] == "applied"
    assert (
        summary["seaweedfs"]["service"]["resource_id"] == "dokploy-compose:cmp-1:seaweedfs-service"
    )
    assert (
        summary["seaweedfs"]["persistent_data"]["resource_id"]
        == "dokploy-compose:cmp-1:seaweedfs-data"
    )
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert loaded_state.ownership_ledger is not None
