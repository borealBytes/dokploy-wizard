"""Typed models for the Headscale runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HeadscaleManagedResource:
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
class HeadscaleHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class HeadscaleResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: HeadscaleManagedResource | None
    secret_refs: tuple[str, ...]
    health_check: HeadscaleHealthCheck | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "hostname": self.hostname,
            "notes": list(self.notes),
            "outcome": self.outcome,
            "secret_refs": list(self.secret_refs),
            "service": None if self.service is None else self.service.to_dict(),
        }


@dataclass(frozen=True)
class HeadscalePhase:
    result: HeadscaleResult
    service_resource_id: str | None


@dataclass(frozen=True)
class HeadscaleResourceRecord:
    resource_id: str
    resource_name: str
