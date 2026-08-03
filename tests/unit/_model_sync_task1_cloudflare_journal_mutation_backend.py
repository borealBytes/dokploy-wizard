from __future__ import annotations

from typing import assert_never

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import JournalOperationKind


class _MutationBackend:
    """In-memory provider double that retains writes across an interrupted process."""

    def __init__(self, *, drift_tunnel_response: bool = False) -> None:
        self.tunnels: dict[str, CloudflareTunnel] = {}
        self.records: dict[str, CloudflareDnsRecord] = {}
        self.apps: dict[str, CloudflareAccessApplication] = {}
        self.policies: dict[str, CloudflareAccessPolicy] = {}
        self.ingress: tuple[dict[str, object], ...] = ()
        self.tunnel_creates = 0
        self.dns_creates = 0
        self.application_creates = 0
        self.policy_creates = 0
        self.configuration_updates = 0
        self.drift_tunnel_response = drift_tunnel_response

    def validate_account_access(self, _account_id: str) -> None:
        return None

    def resolve_zone_id(self, _account_id: str, zone_name: str) -> str | None:
        return f"zone-{zone_name}"

    def validate_zone_access(self, _zone_id: str) -> None:
        return None

    def find_tunnel_by_name(self, _account_id: str, name: str) -> CloudflareTunnel | None:
        return next((tunnel for tunnel in self.tunnels.values() if tunnel.name == name), None)

    def list_tunnels_by_name(self, _account_id: str, name: str) -> tuple[CloudflareTunnel, ...]:
        return tuple(tunnel for tunnel in self.tunnels.values() if tunnel.name == name)

    def create_tunnel(self, _account_id: str, name: str) -> CloudflareTunnel:
        self.tunnel_creates += 1
        tunnel = CloudflareTunnel(f"tunnel-{self.tunnel_creates}", name)
        self.tunnels[tunnel.tunnel_id] = tunnel
        return tunnel

    def get_tunnel(self, _account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        tunnel = self.tunnels.get(tunnel_id)
        if tunnel is None or not self.drift_tunnel_response:
            return tunnel
        return CloudflareTunnel(tunnel.tunnel_id, "foreign-tunnel")

    def get_tunnel_token(self, _account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def get_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        return self.ingress

    def update_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        self.configuration_updates += 1
        self.ingress = ingress

    def list_dns_records(
        self,
        _zone_id: str,
        *,
        hostname: str,
        record_type: str | None,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        records = tuple(record for record in self.records.values() if record.name == hostname)
        if record_type is not None:
            records = tuple(record for record in records if record.record_type == record_type)
        return (
            records
            if content is None
            else tuple(record for record in records if record.content == content)
        )

    def create_dns_record(
        self, _zone_id: str, *, hostname: str, content: str, proxied: bool
    ) -> CloudflareDnsRecord:
        self.dns_creates += 1
        record = CloudflareDnsRecord(f"dns-{self.dns_creates}", hostname, "CNAME", content, proxied)
        self.records[record.record_id] = record
        return record

    def get_dns_record(self, _zone_id: str, record_id: str) -> CloudflareDnsRecord | None:
        return self.records.get(record_id)

    def update_dns_record(
        self,
        _zone_id: str,
        *,
        record_id: str,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        record = CloudflareDnsRecord(record_id, hostname, "CNAME", content, proxied)
        self.records[record_id] = record
        return record

    def list_certificate_packs(self, _zone_id: str) -> tuple[CloudflareCertificatePack, ...]:
        return ()

    def order_advanced_certificate_pack(
        self, _zone_id: str, *, hosts: tuple[str, ...]
    ) -> CloudflareCertificatePack:
        return CloudflareCertificatePack("certificate-1", "advanced", "active", hosts)

    def get_access_identity_provider(
        self, _account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        return CloudflareAccessIdentityProvider(provider_id, "One-time PIN login", "onetimepin")

    def list_access_identity_providers(
        self, _account_id: str
    ) -> tuple[CloudflareAccessIdentityProvider, ...]:
        return (
            CloudflareAccessIdentityProvider("otp-provider", "One-time PIN login", "onetimepin"),
        )

    def find_access_identity_provider_by_name(
        self, _account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        return CloudflareAccessIdentityProvider("otp-provider", name, "onetimepin")

    def create_access_identity_provider(
        self, _account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        return CloudflareAccessIdentityProvider("otp-provider", name, "onetimepin")

    def delete_access_identity_provider(self, _account_id: str, _provider_id: str) -> None:
        return None

    def find_access_application_by_domain(
        self, _account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        return next((app for app in self.apps.values() if app.domain == domain), None)

    def list_access_applications_by_domain(
        self, _account_id: str, domain: str
    ) -> tuple[CloudflareAccessApplication, ...]:
        return tuple(app for app in self.apps.values() if app.domain == domain)

    def create_access_application(
        self,
        _account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
        self.application_creates += 1
        app = CloudflareAccessApplication(
            f"app-{self.application_creates}",
            name,
            domain,
            "self_hosted",
            allowed_identity_provider_ids,
        )
        self.apps[app.app_id] = app
        return app

    def delete_access_application(self, _account_id: str, app_id: str) -> None:
        self.apps.pop(app_id, None)

    def get_access_application(
        self, _account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        return self.apps.get(app_id)

    def find_access_policy_by_name(
        self, _account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
        return next(
            (
                policy
                for policy in self.policies.values()
                if policy.app_id == app_id and policy.name == name
            ),
            None,
        )

    def list_access_policies_by_name(
        self, _account_id: str, app_id: str, name: str
    ) -> tuple[CloudflareAccessPolicy, ...]:
        return tuple(
            policy
            for policy in self.policies.values()
            if policy.app_id == app_id and policy.name == name
        )

    def create_access_policy(
        self, _account_id: str, *, app_id: str, name: str, emails: tuple[str, ...]
    ) -> CloudflareAccessPolicy:
        self.policy_creates += 1
        policy = CloudflareAccessPolicy(
            f"policy-{self.policy_creates}", app_id, name, "allow", emails
        )
        self.policies[policy.policy_id] = policy
        return policy

    def get_access_policy(
        self, _account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        policy = self.policies.get(policy_id)
        return policy if policy is not None and policy.app_id == app_id else None

    def create_count(self, kind: JournalOperationKind) -> int:
        match kind:
            case JournalOperationKind.ACCESS_APPLICATION:
                return self.application_creates
            case JournalOperationKind.ACCESS_POLICY:
                return self.policy_creates
            case JournalOperationKind.DNS_RECORD:
                return self.dns_creates
            case JournalOperationKind.TUNNEL_CONFIGURATION:
                return self.configuration_updates
            case JournalOperationKind.TUNNEL:
                return self.tunnel_creates
            case unreachable:
                assert_never(unreachable)
