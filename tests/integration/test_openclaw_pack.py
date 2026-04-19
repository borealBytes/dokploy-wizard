# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import dokploy_wizard.cli
from dokploy_wizard.cli import run_install_flow, run_modify_flow
from dokploy_wizard.core import SharedCoreResourceRecord
from dokploy_wizard.dokploy.openclaw import DokployOpenClawBackend
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.matrix import MatrixResourceRecord
from dokploy_wizard.packs.openclaw import OpenClawError, OpenClawResourceRecord
from dokploy_wizard.state import RawEnvInput, load_state_dir, write_ownership_ledger
from tests.unit.test_openclaw_pack import FakeDokployOpenClawApi

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
        self.existing_tunnel = CloudflareTunnel(tunnel_id="openclaw-tunnel", name=tunnel_name)
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
            resource_id="shared-network-1",
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
        self.postgres = SharedCoreResourceRecord(
            resource_id="shared-postgres-1",
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
        self.redis = SharedCoreResourceRecord(
            resource_id="shared-redis-1",
            resource_name=resource_name,
        )
        return self.redis


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None
    health_ok: bool = True

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
        return self.health_ok


@dataclass
class FakeOpenClawBackend:
    existing_service: OpenClawResourceRecord | None = None
    health_ok: bool = True
    create_calls: int = 0
    update_calls: int = 0
    last_requested_replicas: int | None = None
    last_health_url: str | None = None

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
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
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self.create_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self.update_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service
        self.last_health_url = url
        return self.health_ok


@dataclass
class RecordingOpenClawBackend(FakeOpenClawBackend):
    init_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMatrixBackend:
    existing_service: MatrixResourceRecord | None = None
    existing_data: MatrixResourceRecord | None = None
    health_ok: bool = True

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
        self.existing_data = MatrixResourceRecord(
            resource_id="matrix-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


def test_install_reconciles_openclaw_and_persists_slot_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "openclaw-matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        openclaw_backend=FakeOpenClawBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["openclaw"]["outcome"] == "applied"
    assert summary["openclaw"]["variant"] == "openclaw"
    assert summary["openclaw"]["hostname"] == "openclaw.example.com"
    assert summary["openclaw"]["channels"] == ["matrix", "telegram"]
    assert summary["openclaw"]["service"]["resource_name"] == "openclaw-stack-openclaw"
    assert summary["openclaw"]["health_check"]["passed"] is True
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "matrix",
        "openclaw",
        "cloudflare_access",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:matrix.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:openclaw.example.com"),
        ("shared_core_network", "stack:openclaw-stack:shared-network"),
        ("shared_core_postgres", "stack:openclaw-stack:shared-postgres"),
        ("shared_core_redis", "stack:openclaw-stack:shared-redis"),
        ("matrix_service", "stack:openclaw-stack:matrix-service"),
        ("matrix_data", "stack:openclaw-stack:matrix-data"),
        ("openclaw_service", "stack:openclaw-stack:openclaw"),
    }


def test_install_rerun_reuses_owned_advisor_service(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    openclaw_backend = FakeOpenClawBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "openclaw-matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        openclaw_backend=openclaw_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "openclaw-matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="openclaw-tunnel",
                name="openclaw-stack-tunnel",
            ),
            dns_records={
                "dokploy.example.com": CloudflareDnsRecord(
                    record_id="dns-dokploy.example.com",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="openclaw-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "headscale.example.com": CloudflareDnsRecord(
                    record_id="dns-headscale.example.com",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="openclaw-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "matrix.example.com": CloudflareDnsRecord(
                    record_id="dns-matrix.example.com",
                    name="matrix.example.com",
                    record_type="CNAME",
                    content="openclaw-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "openclaw.example.com": CloudflareDnsRecord(
                    record_id="dns-openclaw.example.com",
                    name="openclaw.example.com",
                    record_type="CNAME",
                    content="openclaw-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            },
        ),
        shared_core_backend=FakeSharedCoreBackend(
            network=SharedCoreResourceRecord(
                resource_id="shared-network-1",
                resource_name="openclaw-stack-shared",
            ),
            postgres=SharedCoreResourceRecord(
                resource_id="shared-postgres-1",
                resource_name="openclaw-stack-shared-postgres",
            ),
            redis=SharedCoreResourceRecord(
                resource_id="shared-redis-1",
                resource_name="openclaw-stack-shared-redis",
            ),
        ),
        headscale_backend=FakeHeadscaleBackend(
            existing_service=HeadscaleResourceRecord(
                resource_id="headscale-service-1",
                resource_name="openclaw-stack-headscale",
            )
        ),
        matrix_backend=FakeMatrixBackend(
            existing_service=MatrixResourceRecord(
                resource_id="matrix-service-1",
                resource_name="openclaw-stack-matrix",
            ),
            existing_data=MatrixResourceRecord(
                resource_id="matrix-data-1",
                resource_name="openclaw-stack-matrix-data",
            ),
        ),
        openclaw_backend=openclaw_backend,
    )

    assert summary["openclaw"]["outcome"] == "already_present"
    assert summary["openclaw"]["service"]["action"] == "reuse_owned"
    assert openclaw_backend.create_calls == 1


def test_install_modify_updates_owned_openclaw_service(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "modify-openclaw.env"
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    headscale_backend = FakeHeadscaleBackend()
    matrix_backend = FakeMatrixBackend()
    openclaw_backend = FakeOpenClawBackend()

    initial_raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "openclaw-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "ENABLE_MATRIX": "true",
            "OPENCLAW_CHANNELS": "telegram,matrix",
            "OPENCLAW_REPLICAS": "1",
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
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "api-key-123",
            "CLOUDFLARE_API_TOKEN": "token-123",
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
            "CLOUDFLARE_ZONE_ID": "zone-123",
            "CLOUDFLARE_TUNNEL_NAME": "openclaw-stack-tunnel",
            "HEADSCALE_TAILNET_DOMAIN": "tailnet.example.com",
            "HEADSCALE_ACME_EMAIL": "admin@example.com",
            "HEADSCALE_OIDC_ISSUER_URL": "https://auth.example.com/application/o/headscale/",
            "HEADSCALE_OIDC_CLIENT_ID": "headscale-client",
            "HEADSCALE_OIDC_CLIENT_SECRET": "headscale-secret",
            "HEADSCALE_OIDC_STRIP_EMAIL_DOMAIN": "true",
            "MATRIX_SIGNUP_SECRET": "signup-secret",
            "MATRIX_OIDC_ISSUER_URL": "https://auth.example.com/application/o/matrix/",
            "MATRIX_OIDC_CLIENT_ID": "matrix-client",
            "MATRIX_OIDC_CLIENT_SECRET": "matrix-secret",
        },
    )
    modified_raw_env = RawEnvInput(
        format_version=1,
        values={**initial_raw_env.values, "OPENCLAW_REPLICAS": "3"},
    )

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=initial_raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        openclaw_backend=openclaw_backend,
    )

    summary = run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=modified_raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        openclaw_backend=openclaw_backend,
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["openclaw"]["outcome"] == "applied"
    assert summary["openclaw"]["replicas"] == 3
    assert summary["openclaw"]["service"]["action"] == "update_owned"
    assert openclaw_backend.update_calls == 1
    assert openclaw_backend.last_requested_replicas == 3
    assert loaded_state.ownership_ledger is not None
    assert any(
        resource.resource_type == "openclaw_service"
        and resource.scope == "stack:openclaw-stack:openclaw"
        for resource in loaded_state.ownership_ledger.resources
    )


def test_install_rerun_fails_when_openclaw_service_is_manual_and_unowned(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    headscale_backend = FakeHeadscaleBackend()
    matrix_backend = FakeMatrixBackend()
    openclaw_backend = FakeOpenClawBackend()

    run_install_flow(
        env_file=FIXTURES_DIR / "openclaw-matrix.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        openclaw_backend=openclaw_backend,
    )

    loaded_state = load_state_dir(state_dir)
    assert loaded_state.ownership_ledger is not None
    write_ownership_ledger(
        state_dir,
        loaded_state.ownership_ledger.__class__(
            format_version=loaded_state.ownership_ledger.format_version,
            resources=tuple(
                resource
                for resource in loaded_state.ownership_ledger.resources
                if resource.resource_type != "openclaw_service"
            ),
        ),
    )

    with pytest.raises(OpenClawError, match="requires migration"):
        run_install_flow(
            env_file=FIXTURES_DIR / "openclaw-matrix.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=networking_backend,
            shared_core_backend=shared_core_backend,
            headscale_backend=headscale_backend,
            matrix_backend=matrix_backend,
            openclaw_backend=openclaw_backend,
        )


def test_install_reconciles_my_farm_advisor_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "my-farm-advisor.env"
    recording_backend = RecordingOpenClawBackend()

    def _build_backend(**kwargs: Any) -> RecordingOpenClawBackend:
        recording_backend.init_kwargs = dict(kwargs)
        return recording_backend

    monkeypatch.setattr(dokploy_wizard.cli, "DokployOpenClawBackend", _build_backend)
    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "farm-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
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
                "DOKPLOY_API_URL": "https://dokploy.example.com/api",
                "DOKPLOY_API_KEY": "api-key-123",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "CLOUDFLARE_TUNNEL_NAME": "farm-stack-tunnel",
                "ADVISOR_MODEL_PROVIDER": "ollama",
                "ADVISOR_MODEL_NAME": "llama3.1:8b",
                "ADVISOR_TRUSTED_PROXIES": "10.0.0.0/8",
                "ADVISOR_NVIDIA_VISIBLE_DEVICES": "GPU-1",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
    )

    assert summary["my_farm_advisor"]["variant"] == "my-farm-advisor"
    assert summary["my_farm_advisor"]["hostname"] == "farm.example.com"
    assert summary["my_farm_advisor"]["health_check"]["url"] == "https://farm.example.com/healthz"
    assert summary["my_farm_advisor"]["template_path"].endswith(
        "templates/packs/my-farm-advisor.compose.yaml"
    )
    assert recording_backend.init_kwargs["stack_name"] == "farm-stack"
    assert recording_backend.init_kwargs["model_provider"] == "ollama"
    assert recording_backend.init_kwargs["model_name"] == "llama3.1:8b"
    assert recording_backend.init_kwargs["trusted_proxies"] == "10.0.0.0/8"
    assert recording_backend.init_kwargs["nvidia_visible_devices"] == "GPU-1"
    assert recording_backend.last_health_url == "https://farm.example.com/healthz"


def test_install_passes_nexa_env_into_dokploy_openclaw_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "openclaw-nexa.env"
    recording_backend = RecordingOpenClawBackend()

    def _build_backend(**kwargs: Any) -> RecordingOpenClawBackend:
        recording_backend.init_kwargs = dict(kwargs)
        return recording_backend

    monkeypatch.setattr(dokploy_wizard.cli, "DokployOpenClawBackend", _build_backend)
    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._docker_container_is_up", lambda service_name: False)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._local_https_health_check", lambda url: True)

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openclaw-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
                "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET": "office-shared-secret",
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
                "DOKPLOY_API_URL": "https://dokploy.example.com/api",
                "DOKPLOY_API_KEY": "api-key-123",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "CLOUDFLARE_TUNNEL_NAME": "openclaw-stack-tunnel",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "admin@example.com",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        openclaw_backend=None,
    )

    assert summary["openclaw"]["variant"] == "openclaw"
    assert recording_backend.init_kwargs["openclaw_nexa_env"] == {
        "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
        "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET": "office-shared-secret",
    }


def test_install_renders_internal_nexa_runtime_sidecar_into_openclaw_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "openclaw-nexa-compose.env"
    api = FakeDokployOpenClawApi()

    def _build_backend(**kwargs: Any) -> DokployOpenClawBackend:
        kwargs["client"] = api
        return DokployOpenClawBackend(**kwargs)

    monkeypatch.setattr(dokploy_wizard.cli, "DokployOpenClawBackend", _build_backend)
    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._docker_container_is_up", lambda service_name: False)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._local_https_health_check", lambda url: True)

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openclaw-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
                "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
                "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
                "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "qdrant-api-key",
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
                "DOKPLOY_API_URL": "https://dokploy.example.com/api",
                "DOKPLOY_API_KEY": "api-key-123",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "CLOUDFLARE_TUNNEL_NAME": "openclaw-stack-tunnel",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "admin@example.com",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        openclaw_backend=None,
    )

    compose = api.last_create_compose_file
    assert summary["openclaw"]["outcome"] == "applied"
    assert compose is not None
    assert "  nexa-runtime:\n" in compose
    assert 'image: local/dokploy-wizard-nexa-runtime:latest' in compose
    assert "context: ." in compose
    assert "dockerfile: docker/nexa-runtime/Dockerfile" in compose
    assert "openclaw-stack-openclaw-data:/mnt/openclaw" in compose
    assert 'DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH: "/mnt/openclaw/.nexa/runtime-contract.json"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH: "/mnt/openclaw/workspace/nexa/contract.json"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_STATE_DIR: "/mnt/openclaw/.nexa/state"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_WORKER_MODE: "queue"' in compose
    assert "traefik.http.routers.nexa-runtime" not in compose
    assert "ports:" not in compose


def test_install_fails_when_advisor_slot_health_check_does_not_pass(tmp_path: Path) -> None:
    with pytest.raises(OpenClawError, match="health check failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "openclaw-matrix.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(),
            matrix_backend=FakeMatrixBackend(),
            openclaw_backend=FakeOpenClawBackend(health_ok=False),
        )
