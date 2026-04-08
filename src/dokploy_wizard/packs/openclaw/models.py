"""Typed models for the advisor-slot runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenClawManagedResource:
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
class OpenClawHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class OpenClawResult:
    outcome: str
    enabled: bool
    variant: str | None
    hostname: str | None
    channels: tuple[str, ...]
    replicas: int | None
    template_path: str | None
    service: OpenClawManagedResource | None
    secret_refs: tuple[str, ...]
    health_check: OpenClawHealthCheck | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "enabled": self.enabled,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "hostname": self.hostname,
            "notes": list(self.notes),
            "outcome": self.outcome,
            "replicas": self.replicas,
            "secret_refs": list(self.secret_refs),
            "service": None if self.service is None else self.service.to_dict(),
            "template_path": self.template_path,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class OpenClawPhase:
    result: OpenClawResult
    service_resource_id: str | None


@dataclass(frozen=True)
class OpenClawResourceRecord:
    resource_id: str
    resource_name: str
    replicas: int
