# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleError, HeadscaleResourceRecord
from dokploy_wizard.state import load_state_dir

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool = True
    healthy_after_install: bool = True
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
    create_tunnel_calls: int = 0
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
        self.create_tunnel_calls += 1
        self.existing_tunnel = CloudflareTunnel(tunnel_id="resume-tunnel", name=tunnel_name)
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
            resource_id="resume-headscale-service",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


def test_resume_uses_persisted_phase_prefix_after_failure(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    networking_backend = FakeCloudflareBackend()
    failing_headscale = FakeHeadscaleBackend(health_ok=False)

    try:
        run_install_flow(
            env_file=FIXTURES_DIR / "headscale.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(),
            networking_backend=networking_backend,
            headscale_backend=failing_headscale,
        )
    except HeadscaleError:
        pass
    else:
        raise AssertionError("Expected the first install attempt to fail during headscale health.")

    loaded_after_failure = load_state_dir(state_dir)
    assert loaded_after_failure.applied_state is not None
    assert loaded_after_failure.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    )

    successful_headscale = FakeHeadscaleBackend(health_ok=True)
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "headscale.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(),
        networking_backend=networking_backend,
        headscale_backend=successful_headscale,
    )

    loaded_after_resume = load_state_dir(state_dir)
    assert summary["lifecycle"]["mode"] == "resume"
    assert summary["lifecycle"]["start_phase"] == "headscale"
    assert summary["networking"]["outcome"] == "already_present"
    assert summary["headscale"]["outcome"] == "applied"
    assert successful_headscale.create_calls == 1
    assert loaded_after_resume.applied_state is not None
    assert loaded_after_resume.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
