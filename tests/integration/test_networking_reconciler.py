# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareError,
    CloudflareTunnel,
)
from dokploy_wizard.preflight import HostFacts, PreflightCheck, PreflightReport, ResourceProfile
from dokploy_wizard.packs.coder import CoderResourceRecord
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.nextcloud import (
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudResourceRecord,
    TalkRuntime,
)
from dokploy_wizard.packs.openclaw import OpenClawResourceRecord
from dokploy_wizard.state import load_state_dir

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
class FakeCoderBackend:
    existing_service: CoderResourceRecord | None = None
    existing_data: CoderResourceRecord | None = None

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> CoderResourceRecord:
        resource_name = str(kwargs["resource_name"])
        self.existing_service = CoderResourceRecord(
            resource_id="coder-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> CoderResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        self.existing_data = CoderResourceRecord(
            resource_id="coder-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service, url
        return True

    def ensure_application_ready(self) -> tuple[str, ...]:
        return ()


@dataclass
class FakeCloudflareBackend:
    account_ok: bool = True
    zone_ok: bool = True
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, CloudflareDnsRecord] = field(default_factory=dict)
    certificate_packs: dict[str, CloudflareCertificatePack] = field(default_factory=dict)
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)
    create_tunnel_calls: int = 0
    create_dns_calls: int = 0
    ordered_certificate_hosts: list[tuple[str, ...]] = field(default_factory=list)
    update_tunnel_configuration_calls: list[tuple[str, str, tuple[dict[str, object], ...]]] = field(
        default_factory=list
    )

    def validate_account_access(self, account_id: str) -> None:
        if not self.account_ok:
            raise CloudflareError("account scope validation failed")

    def validate_zone_access(self, zone_id: str) -> None:
        if not self.zone_ok:
            raise CloudflareError("zone scope validation failed")

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        self.create_tunnel_calls += 1
        self.existing_tunnel = CloudflareTunnel(tunnel_id="tunnel-created", name=tunnel_name)
        return self.existing_tunnel

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        self.update_tunnel_configuration_calls.append((account_id, tunnel_id, ingress))

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
        self.create_dns_calls += 1
        record = CloudflareDnsRecord(
            record_id=f"dns-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )
        self.dns_records[hostname] = record
        return record

    def list_certificate_packs(self, zone_id: str) -> tuple[CloudflareCertificatePack, ...]:
        return tuple(self.certificate_packs.values())

    def order_advanced_certificate_pack(
        self, zone_id: str, *, hosts: tuple[str, ...]
    ) -> CloudflareCertificatePack:
        self.ordered_certificate_hosts.append(hosts)
        pack = CloudflareCertificatePack(
            pack_id=f"cert-{hosts[-1]}",
            pack_type="advanced",
            status="active",
            hosts=hosts,
        )
        self.certificate_packs[hosts[-1]] = pack
        return pack

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        if self.access_provider is not None and self.access_provider.provider_id == provider_id:
            return self.access_provider
        return None

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        if self.access_provider is not None and self.access_provider.name == name:
            return self.access_provider
        return None

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        self.access_provider = CloudflareAccessIdentityProvider(
            provider_id="otp-provider-1",
            name=name,
            provider_type="onetimepin",
        )
        return self.access_provider

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        return next((item for item in self.access_apps.values() if item.app_id == app_id), None)

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        return self.access_apps.get(domain)

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
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
        return self.access_policies.get(app_id)

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
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

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        del admin_user


@dataclass
class FakeOpenClawBackend:
    existing_service: OpenClawResourceRecord | None = None

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
        del hostname, template_path, variant, channels, replicas, secret_refs
        self.existing_service = OpenClawResourceRecord(
            resource_id="openclaw-service-1",
            resource_name=resource_name,
            replicas=1,
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
        del hostname, template_path, variant, channels, replicas, secret_refs
        self.existing_service = OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
            replicas=1,
        )
        return self.existing_service

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service, url
        return True


def test_install_non_dry_run_reconciles_networking_and_persists_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    bootstrap_backend = FakeDokployBackend(healthy_before_install=True, healthy_after_install=True)
    networking_backend = FakeCloudflareBackend()

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=bootstrap_backend,
        networking_backend=networking_backend,
        headscale_backend=FakeHeadscaleBackend(),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["networking"]["outcome"] == "applied"
    assert summary["networking"]["tunnel"]["action"] == "create"
    assert networking_backend.create_tunnel_calls == 1
    assert networking_backend.create_dns_calls == 2
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
        ("headscale_service", "stack:wizard-stack:headscale"),
    }


def test_install_rerun_uses_ledger_owned_networking_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    bootstrap_backend = FakeDokployBackend(healthy_before_install=True, healthy_after_install=True)
    first_networking_backend = FakeCloudflareBackend()
    headscale_backend = FakeHeadscaleBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=bootstrap_backend,
        networking_backend=first_networking_backend,
        headscale_backend=headscale_backend,
    )

    second_networking_backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-created", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": CloudflareDnsRecord(
                record_id="dns-dokploy.example.com",
                name="dokploy.example.com",
                record_type="CNAME",
                content="tunnel-created.cfargotunnel.com",
                proxied=True,
            ),
            "headscale.example.com": CloudflareDnsRecord(
                record_id="dns-headscale.example.com",
                name="headscale.example.com",
                record_type="CNAME",
                content="tunnel-created.cfargotunnel.com",
                proxied=True,
            ),
        },
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=bootstrap_backend,
        networking_backend=second_networking_backend,
        headscale_backend=headscale_backend,
    )

    assert summary["networking"]["outcome"] == "already_present"
    assert summary["networking"]["tunnel"]["action"] == "reuse_owned"
    assert second_networking_backend.create_tunnel_calls == 0
    assert second_networking_backend.create_dns_calls == 0


def test_install_applies_access_only_for_advisor_hostnames(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "access.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_BOOTSTRAP_MOCK_API_KEY=dokp-test-key",
                "ENABLE_OPENCLAW=true",
                "CLOUDFLARE_ACCESS_OTP_EMAILS=owner@example.com,ops@example.com",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=6",
                "HOST_MEMORY_GB=12",
                "HOST_DISK_GB=150",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
                "HOST_ENVIRONMENT=local",
                "DOKPLOY_BOOTSTRAP_HEALTHY=true",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "CLOUDFLARE_ZONE_ID=zone-123",
                "CLOUDFLARE_MOCK_ACCOUNT_OK=true",
                "CLOUDFLARE_MOCK_ZONE_OK=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = FakeCloudflareBackend()
    nextcloud_backend = FakeNextcloudBackend()

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=nextcloud_backend,
        openclaw_backend=FakeOpenClawBackend(),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["cloudflare_access"]["outcome"] == "applied"
    assert [item["hostname"] for item in summary["cloudflare_access"]["applications"]] == [
        "openclaw.example.com"
    ]
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps[:5] == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "openclaw",
    )
    assert loaded_state.ownership_ledger is not None
    assert {resource.resource_type for resource in loaded_state.ownership_ledger.resources} >= {
        "cloudflare_access_otp_provider",
        "cloudflare_access_application",
        "cloudflare_access_policy",
    }


def test_install_keeps_onlyoffice_out_of_access_while_my_farm_stays_managed_by_repo_routing(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "access-nextcloud-farm.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_BOOTSTRAP_MOCK_API_KEY=dokp-test-key",
                "ENABLE_NEXTCLOUD=true",
                "ENABLE_MY_FARM_ADVISOR=true",
                "MY_FARM_ADVISOR_CHANNELS=telegram",
                "MY_FARM_ADVISOR_OPENROUTER_API_KEY=farm-openrouter-key",
                "CLOUDFLARE_ACCESS_OTP_EMAILS=owner@example.com,ops@example.com",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=4",
                "HOST_MEMORY_GB=12",
                "HOST_DISK_GB=150",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
                "HOST_ENVIRONMENT=local",
                "DOKPLOY_BOOTSTRAP_HEALTHY=true",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "CLOUDFLARE_ZONE_ID=zone-123",
                "CLOUDFLARE_MOCK_ACCOUNT_OK=true",
                "CLOUDFLARE_MOCK_ZONE_OK=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = FakeCloudflareBackend()
    nextcloud_backend = FakeNextcloudBackend()

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=nextcloud_backend,
        openclaw_backend=FakeOpenClawBackend(),
    )

    dns_hostnames = [item["hostname"] for item in summary["networking"]["dns_records"]]
    access_hostnames = [item["hostname"] for item in summary["cloudflare_access"]["applications"]]

    assert "farm.example.com" in dns_hostnames
    assert "office.example.com" in dns_hostnames
    assert access_hostnames == ["farm.example.com"]
    assert "office.example.com" not in access_hostnames


def test_install_includes_coder_root_and_wildcard_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    state_dir = tmp_path / "state"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_CODER=true",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "CLOUDFLARE_ZONE_ID=zone-123",
                "CLOUDFLARE_MOCK_ACCOUNT_OK=true",
                "CLOUDFLARE_MOCK_ZONE_OK=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = FakeCloudflareBackend()
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **_: (None, None),
    )
    monkeypatch.setattr(
        cli,
        "_run_preflight_report",
        lambda **_: PreflightReport(
            host_facts=HostFacts(
                distribution_id="ubuntu",
                version_id="24.04",
                cpu_count=8,
                memory_gb=16,
                disk_gb=200,
                disk_path="/var/lib/docker",
                docker_installed=True,
                docker_daemon_reachable=True,
                ports_in_use=(),
                environment_classification="local",
                hostname="test-host",
            ),
            required_profile=ResourceProfile(
                name="Recommended",
                minimum_vcpu=4,
                minimum_memory_gb=8,
                minimum_disk_gb=100,
            ),
            checks=(PreflightCheck(name="preflight", status="pass", detail="ok"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **kwargs: None)

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=backend,
        headscale_backend=FakeHeadscaleBackend(),
        coder_backend=FakeCoderBackend(),
    )

    dns_hostnames = [item["hostname"] for item in summary["networking"]["dns_records"]]

    assert "coder.example.com" in dns_hostnames
    assert "*.coder.example.com" in dns_hostnames
    assert backend.ordered_certificate_hosts == [
        ("example.com", "coder.example.com", "*.coder.example.com")
    ]


def test_install_programs_tunnel_ingress_to_local_https(tmp_path: Path) -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel")
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=backend,
        headscale_backend=FakeHeadscaleBackend(),
    )

    assert summary["networking"]["connector"] is None
    assert len(backend.update_tunnel_configuration_calls) == 1
    account_id, tunnel_id, ingress = backend.update_tunnel_configuration_calls[0]
    assert account_id == "account-123"
    assert tunnel_id == "tunnel-123"
    assert ingress[0] == {
        "hostname": "dokploy.example.com",
        "service": "https://localhost:443",
        "originRequest": {"noTLSVerify": True},
    }
    assert ingress[-1] == {"service": "http_status:404"}


def test_install_with_coder_wildcard_adds_catchall_tunnel_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel")
    )
    env_file = tmp_path / "cloudflare-coder.env"
    env_file.write_text((FIXTURES_DIR / "cloudflare-valid.env").read_text() + "\nENABLE_CODER=1\n")

    monkeypatch.setattr(
        cli,
        "_run_preflight_report",
        lambda **_: PreflightReport(
            host_facts=HostFacts(
                distribution_id="ubuntu",
                version_id="24.04",
                cpu_count=8,
                memory_gb=16,
                disk_gb=200,
                disk_path="/var/lib/docker",
                docker_installed=True,
                docker_daemon_reachable=True,
                ports_in_use=(),
                environment_classification="local",
                hostname="test-host",
            ),
            required_profile=ResourceProfile(
                name="Recommended",
                minimum_vcpu=4,
                minimum_memory_gb=8,
                minimum_disk_gb=100,
            ),
            checks=(PreflightCheck(name="preflight", status="pass", detail="ok"),),
            advisories=(),
        ),
    )

    run_install_flow(
        env_file=env_file,
        state_dir=tmp_path / "state",
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=backend,
        headscale_backend=FakeHeadscaleBackend(),
        coder_backend=FakeCoderBackend(),
    )

    _, _, ingress = backend.update_tunnel_configuration_calls[0]
    assert ingress[-1] == {
        "service": "https://localhost:443",
        "originRequest": {"noTLSVerify": True},
    }
    assert {"service": "http_status:404"} not in ingress


def test_install_fails_closed_when_zone_scope_is_wrong(tmp_path: Path) -> None:
    with pytest.raises(CloudflareError, match="zone scope validation failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "cloudflare-valid.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(zone_ok=False),
            headscale_backend=FakeHeadscaleBackend(),
        )
