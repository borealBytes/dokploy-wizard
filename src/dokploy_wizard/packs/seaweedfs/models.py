"""Typed SeaweedFS runtime phase models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeaweedFsManagedResource:
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
class SeaweedFsHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {"url": self.url, "passed": self.passed}


@dataclass(frozen=True)
class SeaweedFsResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: SeaweedFsManagedResource | None
    persistent_data: SeaweedFsManagedResource | None
    access_key: str | None
    health_check: SeaweedFsHealthCheck | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "access_key": self.access_key,
            "enabled": self.enabled,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "hostname": self.hostname,
            "notes": list(self.notes),
            "outcome": self.outcome,
            "persistent_data": None
            if self.persistent_data is None
            else self.persistent_data.to_dict(),
            "service": None if self.service is None else self.service.to_dict(),
        }


@dataclass(frozen=True)
class SeaweedFsPhase:
    result: SeaweedFsResult
    service_resource_id: str | None
    data_resource_id: str | None
