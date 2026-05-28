# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareError,
    CloudflareTunnel,
    reconcile_cloudflare_access,
    reconcile_networking,
)
from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    resolve_desired_state,
)


@dataclass
class FakeConnectorRecord:
    resource_id: str
    resource_name: str


@dataclass
class FakeConnectorBackend:
    existing_service: FakeConnectorRecord | None = None
    created: list[tuple[str, str]] = field(default_factory=list)

    def get_service(self, resource_id: str) -> FakeConnectorRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> FakeConnectorRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, *, resource_name: str, tunnel_token: str) -> FakeConnectorRecord:
        self.created.append((resource_name, tunnel_token))
        self.existing_service = FakeConnectorRecord(
            resource_id=f"dokploy-compose:{resource_name}:cloudflared",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: FakeConnectorRecord, url: str) -> bool:
        return (
            url == "https://dokploy.example.com"
            and service.resource_name == "wizard-stack-cloudflared"
        )


@dataclass
class FakeCloudflareBackend:
    account_ok: bool = True
    zone_ok: bool = True
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, tuple[CloudflareDnsRecord, ...]] = field(default_factory=dict)
    certificate_packs: dict[str, CloudflareCertificatePack] = field(default_factory=dict)
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)
    certificate_scope_ok: bool = True
    ordered_certificate_hosts: list[tuple[str, ...]] = field(default_factory=list)
    dns_record_updates: list[tuple[str, str, str, str, bool]] = field(default_factory=list)

    def validate_account_access(self, account_id: str) -> None:
        if not self.account_ok:
            raise CloudflareError(f"account scope failed for {account_id}")

    def validate_zone_access(self, zone_id: str) -> None:
        if not self.zone_ok:
            raise CloudflareError(f"zone scope failed for {zone_id}")

    def resolve_zone_id(self, account_id: str, zone_name: str) -> str | None:
        if not self.zone_ok:
            raise CloudflareError(f"zone scope failed for {zone_name}")
        return f"resolved-{zone_name}"

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

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        del account_id, tunnel_id
        return ()

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        del account_id, tunnel_id, ingress

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str | None,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        records = self.dns_records.get(hostname, ())
        if record_type is not None:
            records = tuple(record for record in records if record.record_type == record_type)
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

    def update_dns_record(
        self,
        zone_id: str,
        *,
        record_id: str,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        self.dns_record_updates.append((zone_id, record_id, hostname, content, proxied))
        updated_record = CloudflareDnsRecord(
            record_id=record_id,
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )
        self.dns_records[hostname] = (updated_record,)
        return updated_record

    def list_certificate_packs(self, zone_id: str) -> tuple[CloudflareCertificatePack, ...]:
        if not self.certificate_scope_ok:
            raise CloudflareError("certificate scope failed")
        return tuple(self.certificate_packs.values())

    def order_advanced_certificate_pack(
        self, zone_id: str, *, hosts: tuple[str, ...]
    ) -> CloudflareCertificatePack:
        if not self.certificate_scope_ok:
            raise CloudflareError("certificate scope failed")
        self.ordered_certificate_hosts.append(hosts)
        pack = CloudflareCertificatePack(
            pack_id=f"created-cert-{hosts[-1]}",
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


def test_networking_uses_domain_derived_stack_name_for_tunnel_when_stack_name_omitted() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "ROOT_DOMAIN": "openmerge.me",
                "ENABLE_CODER": "true",
            },
        )
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeCloudflareBackend(),
    )

    assert desired_state.stack_name == "openmerge-me"
    assert phase.result.tunnel.tunnel_name == "openmerge-me-tunnel"


def test_networking_reuses_compatible_unowned_coder_wildcard_dns() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-coder",
                    name="coder.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "*.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-wildcard",
                    name="*.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.tunnel.action == "reuse_existing"
    assert {
        (record.hostname, record.action, record.record_id)
        for record in phase.result.dns_records
    } >= {("*.example.com", "reuse_existing", "dns-wildcard")}


def test_networking_degrades_conflicting_unowned_coder_wildcard_dns() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-coder",
                    name="coder.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "*.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-wildcard",
                    name="*.example.com",
                    record_type="CNAME",
                    content="other-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    planned = {
        (record.hostname, record.action, record.record_id)
        for record in phase.result.dns_records
    }
    assert ("dokploy.example.com", "reuse_existing", "dns-dokploy") in planned
    assert ("coder.example.com", "reuse_existing", "dns-coder") in planned
    assert not any(record.hostname == "*.example.com" for record in phase.result.dns_records)
    assert phase.dns_resource_ids == {
        "coder.example.com": "dns-coder",
        "dokploy.example.com": "dns-dokploy",
    }
    assert any(
        "Skipped optional Coder DNS" in note
        and "did not adopt, overwrite, delete, or retarget" in note
        for note in phase.result.notes
    )


def test_networking_retargets_stale_coder_control_dns_and_degrades_wildcard_dns() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-coder",
                    name="coder.example.com",
                    record_type="CNAME",
                    content="other-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "*.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-wildcard",
                    name="*.example.com",
                    record_type="CNAME",
                    content="other-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    planned = {
        (record.hostname, record.action, record.record_id)
        for record in phase.result.dns_records
    }
    assert ("dokploy.example.com", "reuse_existing", "dns-dokploy") in planned
    assert ("coder.example.com", "update_existing", "dns-coder") in planned
    assert not any(record.hostname == "*.example.com" for record in phase.result.dns_records)
    assert phase.dns_resource_ids == {
        "coder.example.com": "dns-coder",
        "dokploy.example.com": "dns-dokploy",
    }
    assert backend.dns_record_updates == [
        (
            "zone-123",
            "dns-coder",
            "coder.example.com",
            "tunnel-123.cfargotunnel.com",
            True,
        )
    ]
    assert any(
        "Skipped optional Coder DNS for '*.example.com'" in note
        and "explicit service hostnames continue to be reconciled" in note
        for note in phase.result.notes
    )


def test_networking_degrades_stale_nested_coder_wildcard_dns() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
                "CODER_WILDCARD_SUBDOMAIN": "*.coder",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-coder",
                    name="coder.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "*.coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-nested-wildcard",
                    name="*.coder.example.com",
                    record_type="CNAME",
                    content="old-coder-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert not any(
        record.hostname == "*.coder.example.com" for record in phase.result.dns_records
    )
    assert backend.dns_record_updates == []
    assert "*.coder.example.com" not in phase.dns_resource_ids
    assert any(
        "Skipped optional Coder DNS for '*.coder.example.com'" in note
        and "did not adopt, overwrite, delete, or retarget" in note
        for note in phase.result.notes
    )


def test_networking_retargets_stale_dokploy_tunnel_cname() -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="old-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "headscale.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-headscale",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    dokploy_record = next(
        record for record in phase.result.dns_records if record.hostname == "dokploy.example.com"
    )
    assert dokploy_record.action == "update_existing"
    assert dokploy_record.record_id == "dns-dokploy"
    assert dokploy_record.content == "tunnel-123.cfargotunnel.com"
    assert dokploy_record.proxied is True
    assert phase.dns_resource_ids["dokploy.example.com"] == "dns-dokploy"
    assert backend.dns_record_updates == [
        (
            "zone-123",
            "dns-dokploy",
            "dokploy.example.com",
            "tunnel-123.cfargotunnel.com",
            True,
        )
    ]


def test_networking_retargets_stale_enabled_service_tunnel_cname() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-key",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "farm.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-farm",
                    name="farm.example.com",
                    record_type="CNAME",
                    content="old-farm-tunnel.cfargotunnel.com",
                    proxied=False,
                ),
            ),
        },
    )

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    farm_record = next(
        record for record in phase.result.dns_records if record.hostname == "farm.example.com"
    )
    assert farm_record.action == "update_existing"
    assert farm_record.record_id == "dns-farm"
    assert farm_record.content == "tunnel-123.cfargotunnel.com"
    assert farm_record.proxied is True
    assert backend.dns_record_updates == [
        (
            "zone-123",
            "dns-farm",
            "farm.example.com",
            "tunnel-123.cfargotunnel.com",
            True,
        )
    ]


def test_networking_still_rejects_explicit_service_dns_conflicts() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="example.net",
                    proxied=True,
                ),
            ),
            "coder.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-coder",
                    name="coder.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
            "*.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-wildcard",
                    name="*.example.com",
                    record_type="CNAME",
                    content="other-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    with pytest.raises(
        CloudflareError,
        match=r"Cloudflare already has a conflicting CNAME record for 'dokploy\.example\.com'",
    ):
        reconcile_networking(
            dry_run=True,
            raw_env=_raw_env(),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


def test_networking_still_rejects_multiple_explicit_service_dns_records() -> None:
    backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="tunnel-123", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-dokploy-a",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="old-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                CloudflareDnsRecord(
                    record_id="dns-dokploy-b",
                    name="dokploy.example.com",
                    record_type="A",
                    content="192.0.2.10",
                    proxied=True,
                ),
            ),
            "headscale.example.com": (
                CloudflareDnsRecord(
                    record_id="dns-headscale",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="tunnel-123.cfargotunnel.com",
                    proxied=True,
                ),
            ),
        },
    )

    with pytest.raises(
        CloudflareError,
        match=r"Cloudflare already has a conflicting CNAME record for 'dokploy\.example\.com'",
    ):
        reconcile_networking(
            dry_run=False,
            raw_env=_raw_env(),
            desired_state=_desired_state(),
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
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


def test_networking_creates_cloudflared_connector_for_dokploy_url() -> None:
    connector = FakeConnectorBackend()
    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeCloudflareBackend(),
        connector_backend=connector,
    )

    assert phase.result.connector is not None
    assert phase.result.connector.action == "create"
    assert phase.result.connector.public_url == "https://dokploy.example.com"
    assert phase.result.connector.passed is True
    assert connector.created == [("wizard-stack-cloudflared", "token-created-tunnel")]


def test_networking_default_coder_wildcard_uses_no_fee_root_fallback() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    backend = FakeCloudflareBackend()

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert desired_state.hostnames["coder-wildcard"] == "*.example.com"
    assert backend.ordered_certificate_hosts == []
    assert not any("advanced edge certificate" in note for note in phase.result.notes)


def test_networking_orders_advanced_certificate_for_explicit_nested_coder_wildcard() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
                "CODER_WILDCARD_SUBDOMAIN": "*.coder",
            },
        )
    )
    backend = FakeCloudflareBackend()

    phase = reconcile_networking(
        dry_run=False,
        raw_env=_raw_env(),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert backend.ordered_certificate_hosts == [
        ("example.com", "coder.example.com", "*.coder.example.com")
    ]
    assert any(
        "advanced edge certificate" in note and "*.coder.example.com" in note
        for note in phase.result.notes
    )


def test_networking_fails_when_nested_coder_wildcard_needs_ssl_scope() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
                "CODER_WILDCARD_SUBDOMAIN": "*.coder",
            },
        )
    )

    with pytest.raises(CloudflareError, match="Zone -> SSL and Certificates -> Edit"):
        reconcile_networking(
            dry_run=False,
            raw_env=_raw_env(),
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeCloudflareBackend(certificate_scope_ok=False),
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
                "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "farm-openrouter-key",
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
        "litellm.example.com",
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
            ),
            "litellm.example.com": CloudflareAccessApplication(
                app_id="app-litellm",
                name="LiteLLM Admin protected",
                domain="litellm.example.com",
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
            ),
            "app-litellm": CloudflareAccessPolicy(
                policy_id="policy-litellm",
                app_id="app-litellm",
                name="Allow LiteLLM Admin",
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
                OwnedResource(
                    resource_type="cloudflare_access_application",
                    resource_id="app-litellm",
                    scope="account:account-123:access-app:litellm.example.com",
                ),
                OwnedResource(
                    resource_type="cloudflare_access_policy",
                    resource_id="policy-litellm",
                    scope="account:account-123:access-policy:litellm.example.com",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.otp_provider is not None
    assert phase.result.otp_provider.action == "reuse_owned"
    assert {item.action for item in phase.result.applications} == {"reuse_owned"}
    assert {item.action for item in phase.result.policies} == {"reuse_owned"}


def test_cloudflare_policy_list_parsing_uses_caller_app_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CloudflareApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-123"})
    )
    monkeypatch.setattr(
        backend,
        "_request_json",
        lambda **_: {
            "result": [
                {
                    "created_at": "2026-01-01T00:00:00Z",
                    "decision": "allow",
                    "exclude": [],
                    "id": "policy-123",
                    "include": [{"email": {"email": "Clayton@SuperiorByteWorks.com"}}],
                    "name": "Allow openclaw.example.com",
                    "precedence": 1,
                    "require": [],
                    "reusable": False,
                    "uid": "uid-123",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
        },
    )

    policy = backend.find_access_policy_by_name(
        "account-123", "app-openclaw", "Allow openclaw.example.com"
    )

    assert policy is not None
    assert policy.policy_id == "policy-123"
    assert policy.app_id == "app-openclaw"
    assert policy.emails == ("clayton@superiorbyteworks.com",)


def test_cloudflare_api_backend_patches_dns_records(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = CloudflareApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-123"})
    )
    calls: list[dict[str, object]] = []

    def fake_request_json(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "result": {
                "content": "tunnel-123.cfargotunnel.com",
                "id": "dns-dokploy",
                "name": "dokploy.example.com",
                "proxied": True,
                "type": "CNAME",
            }
        }

    monkeypatch.setattr(backend, "_request_json", fake_request_json)

    record = backend.update_dns_record(
        "zone-123",
        record_id="dns-dokploy",
        hostname="dokploy.example.com",
        content="tunnel-123.cfargotunnel.com",
        proxied=True,
    )

    assert record.record_id == "dns-dokploy"
    assert calls == [
        {
            "method": "PATCH",
            "path": "/zones/zone-123/dns_records/dns-dokploy",
            "body": {
                "content": "tunnel-123.cfargotunnel.com",
                "name": "dokploy.example.com",
                "proxied": True,
                "type": "CNAME",
            },
        }
    ]


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
        openclaw_gateway_token=None,
        openclaw_channels=(),
        openclaw_replicas=None,
        my_farm_advisor_channels=(),
        my_farm_advisor_replicas=None,
        shared_core=build_shared_core_plan("wizard-stack", ()),
    )


def test_resolve_desired_state_defaults_access_email_to_dokploy_admin_for_openclaw() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "openmerge",
                "ROOT_DOMAIN": "openmerge.me",
                "DOKPLOY_ADMIN_EMAIL": "clayton@superiorbyteworks.com",
                "ENABLE_OPENCLAW": "true",
            },
        )
    )

    assert desired_state.cloudflare_access_otp_emails == ("clayton@superiorbyteworks.com",)
