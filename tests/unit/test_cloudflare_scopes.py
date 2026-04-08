# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareError,
    CloudflareTunnel,
    reconcile_cloudflare_access,
    reconcile_networking,
)
from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    resolve_desired_state,
)


@dataclass
class FakeCloudflareBackend:
    account_ok: bool = True
    zone_ok: bool = True
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, tuple[CloudflareDnsRecord, ...]] = field(default_factory=dict)
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)

    def validate_account_access(self, account_id: str) -> None:
        if not self.account_ok:
            raise CloudflareError(f"account scope failed for {account_id}")

    def validate_zone_access(self, zone_id: str) -> None:
        if not self.zone_ok:
            raise CloudflareError(f"zone scope failed for {zone_id}")

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        return CloudflareTunnel(tunnel_id="created-tunnel", name=tunnel_name)

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        records = self.dns_records.get(hostname, ())
        if content is None:
            return records
        return tuple(record for record in records if record.content == content)

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        return CloudflareDnsRecord(
            record_id=f"created-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )

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


def test_networking_rejects_zone_scope_before_planning_dns() -> None:
    with pytest.raises(CloudflareError, match="zone scope failed"):
        reconcile_networking(
            dry_run=True,
            raw_env=_raw_env(),
            desired_state=_desired_state(),
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeCloudflareBackend(zone_ok=False),
        )


def test_networking_reuses_existing_tunnel_and_dns_when_scopes_are_valid() -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-1",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "headscale.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-2",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=True,
        raw_env=_raw_env(),
        desired_state=_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.tunnel.action == "reuse_existing"
    assert phase.result.tunnel.dns_target == "tunnel-123.cfargotunnel.com"
    assert [record.action for record in phase.result.dns_records] == [
        "reuse_existing",
        "reuse_existing",
    ]
    assert phase.result.validation_checks == (
        "account_cloudflare_tunnel_scope_validated",
        "zone_dns_scope_validated",
    )


def test_networking_fails_closed_when_owned_tunnel_drift_is_detected() -> None:
    with pytest.raises(CloudflareError, match="Ownership ledger says the Cloudflare tunnel exists"):
        reconcile_networking(
            dry_run=False,
            raw_env=_raw_env(),
            desired_state=_desired_state(),
            ownership_ledger=OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="cloudflare_tunnel",
                        resource_id="missing-tunnel",
                        scope="account:account-123",
                    ),
                ),
            ),
            backend=FakeCloudflareBackend(existing_tunnel=None),
        )


def test_access_only_targets_advisor_hostnames() -> None:
    desired = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com,ops@example.com",
            },
        )
    )
    backend = FakeCloudflareBackend()

    phase = reconcile_cloudflare_access(
        dry_run=True,
        raw_env=_raw_env(),
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "plan_only"
    assert [item.hostname for item in phase.result.applications] == [
        "openclaw.example.com",
        "farm.example.com",
    ]
    assert all(
        hostname not in {"dokploy.example.com", "headscale.example.com", "matrix.example.com"}
        for hostname in [item.hostname for item in phase.result.applications]
    )


def test_access_rerun_reuses_owned_resources() -> None:
    desired = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
            },
        )
    )
    backend = FakeCloudflareBackend(
        access_provider=CloudflareAccessIdentityProvider(
            provider_id="otp-provider-1",
            name="One-time PIN login",
            provider_type="onetimepin",
        ),
        access_apps={
            "openclaw.example.com": CloudflareAccessApplication(
                app_id="app-openclaw",
                name="openclaw.example.com protected",
                domain="openclaw.example.com",
                app_type="self_hosted",
                allowed_identity_provider_ids=("otp-provider-1",),
            )
        },
        access_policies={
            "app-openclaw": CloudflareAccessPolicy(
                policy_id="policy-openclaw",
                app_id="app-openclaw",
                name="Allow openclaw.example.com",
                decision="allow",
                emails=("owner@example.com",),
            )
        },
    )

    phase = reconcile_cloudflare_access(
        dry_run=True,
        raw_env=_raw_env(),
        desired_state=desired,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="cloudflare_access_otp_provider",
                    resource_id="otp-provider-1",
                    scope="account:account-123:access-otp-provider",
                ),
                OwnedResource(
                    resource_type="cloudflare_access_application",
                    resource_id="app-openclaw",
                    scope="account:account-123:access-app:openclaw.example.com",
                ),
                OwnedResource(
                    resource_type="cloudflare_access_policy",
                    resource_id="policy-openclaw",
                    scope="account:account-123:access-policy:openclaw.example.com",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.otp_provider is not None
    assert phase.result.otp_provider.action == "reuse_owned"
    assert {item.action for item in phase.result.applications} == {"reuse_owned"}
    assert {item.action for item in phase.result.policies} == {"reuse_owned"}


def _raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
            "CLOUDFLARE_API_TOKEN": "token-123",
            "CLOUDFLARE_ZONE_ID": "zone-123",
            "ROOT_DOMAIN": "example.com",
            "STACK_NAME": "wizard-stack",
        },
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
        cloudflare_access_otp_emails=(),
        enabled_features=("dokploy", "headscale"),
        selected_packs=(),
        enabled_packs=(),
        hostnames={
            "dokploy": "dokploy.example.com",
            "headscale": "headscale.example.com",
        },
        seaweedfs_access_key=None,
        seaweedfs_secret_key=None,
        openclaw_channels=(),
        openclaw_replicas=None,
        my_farm_advisor_channels=(),
        my_farm_advisor_replicas=None,
        shared_core=build_shared_core_plan("wizard-stack", ()),
    )
