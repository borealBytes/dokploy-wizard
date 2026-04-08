"""Typed models for the Matrix runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dokploy_wizard.core.models import PackSharedAllocation


@dataclass(frozen=True)
class MatrixManagedResource:
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
class MatrixHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class MatrixResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: MatrixManagedResource | None
    persistent_data: MatrixManagedResource | None
    shared_postgres_service: str | None
    shared_redis_service: str | None
    shared_allocation: PackSharedAllocation | None
    secret_refs: tuple[str, ...]
    health_check: MatrixHealthCheck | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "hostname": self.hostname,
            "notes": list(self.notes),
            "outcome": self.outcome,
            "persistent_data": (
                None if self.persistent_data is None else self.persistent_data.to_dict()
            ),
            "secret_refs": list(self.secret_refs),
            "service": None if self.service is None else self.service.to_dict(),
            "shared_allocation": (
                None if self.shared_allocation is None else self.shared_allocation.to_dict()
            ),
            "shared_postgres_service": self.shared_postgres_service,
            "shared_redis_service": self.shared_redis_service,
        }


@dataclass(frozen=True)
class MatrixPhase:
    result: MatrixResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class MatrixResourceRecord:
    resource_id: str
    resource_name: str
