from __future__ import annotations

from dataclasses import dataclass, field

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareTunnel,
    reconcile_cloudflare_access,
    reconcile_networking,
)
from dokploy_wizard.state import DesiredState, OwnershipLedger, RawEnvInput


@dataclass
class FakeCloudflareBackend:
    existing_tunnel: CloudflareTunnel | None = None
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)
    update_tunnel_configuration_calls: list[tuple[str, str, tuple[dict[str, object], ...]]] = field(
        default_factory=list
    )

    def validate_account_access(self, account_id: str) -> None:
        del account_id

    def validate_zone_access(self, zone_id: str) -> None:
        del zone_id

    def resolve_zone_id(self, account_id: str, zone_name: str) -> str | None:
        del account_id
        return f"resolved-{zone_name}"

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
        tunnel = CloudflareTunnel(tunnel_id="created-tunnel", name=tunnel_name)
        self.existing_tunnel = tunnel
        return tunnel

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        del account_id
        return f"token-{tunnel_id}"

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        del account_id, tunnel_id
        return ()

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
        del zone_id, hostname, record_type, content
        return ()

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        del zone_id
        return CloudflareDnsRecord(
            record_id=f"dns-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )

    def list_certificate_packs(self, zone_id: str) -> tuple[CloudflareCertificatePack, ...]:
        del zone_id
        return ()

    def order_advanced_certificate_pack(
        self, zone_id: str, *, hosts: tuple[str, ...]
    ) -> CloudflareCertificatePack:
        del zone_id, hosts
        raise AssertionError("advanced certificate ordering is not expected in this test")

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
        del account_id, name
        return self.access_policies.get(app_id)

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


def test_litellm_admin_access_planned_without_advisor_packs() -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel")
    )
    raw_env = _raw_env()
    desired_state = _desired_state()

    access_phase = reconcile_cloudflare_access(
        dry_run=True,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    networking_phase = reconcile_networking(
        dry_run=False,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert [item.hostname for item in access_phase.result.applications] == ["litellm.example.com"]
    assert [item.hostname for item in access_phase.result.policies] == ["litellm.example.com"]
    assert access_phase.result.policies[0].emails == ("owner@example.com",)
    assert any(
        "http://wizard-stack-shared-litellm:4000" in note for note in access_phase.result.notes
    )
    assert any("https://litellm.example.com" in note for note in access_phase.result.notes)
    assert all(
        record.hostname != "litellm.example.com" for record in networking_phase.result.dns_records
    )
    _, _, ingress = backend.update_tunnel_configuration_calls[0]
    assert all(rule.get("hostname") != "litellm.example.com" for rule in ingress)


def test_litellm_admin_access_uses_configured_subdomain() -> None:
    backend = FakeCloudflareBackend()

    phase = reconcile_cloudflare_access(
        dry_run=True,
        raw_env=_raw_env({"LITELLM_ADMIN_SUBDOMAIN": "ai-admin"}),
        desired_state=_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert [item.hostname for item in phase.result.applications] == ["ai-admin.example.com"]
    assert any("https://ai-admin.example.com" in note for note in phase.result.notes)


def _raw_env(overrides: dict[str, str] | None = None) -> RawEnvInput:
    values = {
        "CLOUDFLARE_ACCOUNT_ID": "account-123",
        "CLOUDFLARE_API_TOKEN": "token-123",
        "CLOUDFLARE_ZONE_ID": "zone-123",
        "ROOT_DOMAIN": "example.com",
        "STACK_NAME": "wizard-stack",
    }
    if overrides is not None:
        values.update(overrides)
    return RawEnvInput(
        format_version=1,
        values=values,
    )


def _desired_state() -> DesiredState:
    return DesiredState(
        format_version=1,
        stack_name="wizard-stack",
        root_domain="example.com",
        dokploy_url="https://dokploy.example.com",
        dokploy_api_url=None,
        enable_tailscale=False,
        tailscale_hostname=None,
        tailscale_enable_ssh=False,
        tailscale_tags=(),
        tailscale_subnet_routes=(),
        cloudflare_access_otp_emails=("owner@example.com",),
        enabled_features=(),
        selected_packs=(),
        enabled_packs=(),
        hostnames={"dokploy": "dokploy.example.com"},
        seaweedfs_access_key=None,
        seaweedfs_secret_key=None,
        openclaw_gateway_token=None,
        openclaw_channels=(),
        openclaw_replicas=None,
        my_farm_advisor_channels=(),
        my_farm_advisor_replicas=None,
        shared_core=build_shared_core_plan(stack_name="wizard-stack", enabled_packs=()),
    )
