from __future__ import annotations

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)


class CleanupBackend:
    def __init__(self, *, drift_tunnel_response: bool = False) -> None:
        self.tunnels = {"tunnel-1": CloudflareTunnel("tunnel-1", "task1-tunnel")}
        self.records = {
            "dns-1": CloudflareDnsRecord("dns-1", "dp.example.com", "CNAME", "target", True)
        }
        self.apps = {
            "app-1": CloudflareAccessApplication(
                "app-1", "Task 1", "dp.example.com", "self_hosted", ("otp",)
            )
        }
        self.policies = {
            "policy-1": CloudflareAccessPolicy(
                "policy-1", "app-1", "Allow Task 1", "allow", ("owner@example.com",)
            )
        }
        self.deleted: list[str] = []

    def delete_access_policy(self, _account_id: str, _app_id: str, policy_id: str) -> None:
        self.deleted.append(f"policy:{policy_id}")
        self.policies.pop(policy_id, None)

    def get_access_policy(
        self, _account_id: str, _app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        return self.policies.get(policy_id)

    def delete_access_application(self, _account_id: str, app_id: str) -> None:
        self.deleted.append(f"app:{app_id}")
        self.apps.pop(app_id, None)

    def get_access_application(
        self, _account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        return self.apps.get(app_id)

    def delete_dns_record(self, _zone_id: str, record_id: str) -> None:
        self.deleted.append(f"dns:{record_id}")
        self.records.pop(record_id, None)

    def list_dns_records(
        self, _zone_id: str, *, hostname: str, record_type: str | None, content: str | None
    ) -> tuple[CloudflareDnsRecord, ...]:
        del record_type, content
        return tuple(record for record in self.records.values() if record.name == hostname)

    def get_dns_record(self, _zone_id: str, record_id: str) -> CloudflareDnsRecord | None:
        return self.records.get(record_id)

    def delete_tunnel(self, _account_id: str, tunnel_id: str) -> None:
        self.deleted.append(f"tunnel:{tunnel_id}")
        self.tunnels.pop(tunnel_id, None)

    def get_tunnel(self, _account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        return self.tunnels.get(tunnel_id)

    def get_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        return ()
