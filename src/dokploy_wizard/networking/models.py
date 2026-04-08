"""Typed results for the Cloudflare networking phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannedTunnel:
    action: str
    tunnel_id: str
    tunnel_name: str
    dns_target: str

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "dns_target": self.dns_target,
            "tunnel_id": self.tunnel_id,
            "tunnel_name": self.tunnel_name,
        }


@dataclass(frozen=True)
class PlannedDnsRecord:
    action: str
    hostname: str
    record_id: str
    content: str
    proxied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "content": self.content,
            "hostname": self.hostname,
            "proxied": self.proxied,
            "record_id": self.record_id,
        }


@dataclass(frozen=True)
class PlannedAccessIdentityProvider:
    action: str
    provider_id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "name": self.name, "provider_id": self.provider_id}


@dataclass(frozen=True)
class PlannedAccessApplication:
    action: str
    hostname: str
    app_id: str

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "app_id": self.app_id, "hostname": self.hostname}


@dataclass(frozen=True)
class PlannedAccessPolicy:
    action: str
    hostname: str
    policy_id: str
    emails: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "emails": list(self.emails),
            "hostname": self.hostname,
            "policy_id": self.policy_id,
        }


@dataclass(frozen=True)
class NetworkingResult:
    outcome: str
    account_id: str
    zone_id: str
    validation_checks: tuple[str, ...]
    tunnel: PlannedTunnel
    dns_records: tuple[PlannedDnsRecord, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "dns_records": [record.to_dict() for record in self.dns_records],
            "notes": list(self.notes),
            "outcome": self.outcome,
            "tunnel": self.tunnel.to_dict(),
            "validation_checks": list(self.validation_checks),
            "zone_id": self.zone_id,
        }


@dataclass(frozen=True)
class NetworkingPhase:
    result: NetworkingResult
    tunnel_resource_id: str | None
    dns_resource_ids: dict[str, str]


@dataclass(frozen=True)
class AccessResult:
    outcome: str
    account_id: str
    otp_provider: PlannedAccessIdentityProvider | None
    applications: tuple[PlannedAccessApplication, ...]
    policies: tuple[PlannedAccessPolicy, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "applications": [item.to_dict() for item in self.applications],
            "notes": list(self.notes),
            "otp_provider": None if self.otp_provider is None else self.otp_provider.to_dict(),
            "outcome": self.outcome,
            "policies": [item.to_dict() for item in self.policies],
        }


@dataclass(frozen=True)
class AccessPhase:
    result: AccessResult
    provider_resource_id: str | None
    application_resource_ids: dict[str, str]
    policy_resource_ids: dict[str, str]
