"""Typed Tailscale phase models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TailscaleManagedResource:
    action: str
    resource_id: str
    resource_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class TailscaleNodeStatus:
    hostname: str
    online: bool
    login_name: str | None
    ipv4: str | None
    ipv6: str | None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "hostname": self.hostname,
            "online": self.online,
            "login_name": self.login_name,
            "ipv4": self.ipv4,
            "ipv6": self.ipv6,
        }


@dataclass(frozen=True)
class TailscaleResult:
    outcome: str
    enabled: bool
    hostname: str | None
    node: TailscaleManagedResource | None
    ssh_enabled: bool
    tags: tuple[str, ...]
    subnet_routes: tuple[str, ...]
    status: TailscaleNodeStatus | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "hostname": self.hostname,
            "node": None if self.node is None else self.node.to_dict(),
            "notes": list(self.notes),
            "outcome": self.outcome,
            "ssh_enabled": self.ssh_enabled,
            "status": None if self.status is None else self.status.to_dict(),
            "subnet_routes": list(self.subnet_routes),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class TailscalePhase:
    result: TailscaleResult
    node_resource_id: str | None
