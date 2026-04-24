"""Typed models for the Multica runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dokploy_wizard.core.models import SharedPostgresAllocation


@dataclass(frozen=True)
class MulticaResourceRecord:
    resource_id: str
    resource_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class MulticaServiceConfig:
    hostname: str
    api_hostname: str
    postgres_service_name: str
    postgres: SharedPostgresAllocation

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "api_hostname": self.api_hostname,
            "postgres_service_name": self.postgres_service_name,
            "postgres": self.postgres.to_dict(),
        }


@dataclass(frozen=True)
class MulticaPostgresBinding:
    service_name: str
    database_name: str
    user_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "service_name": self.service_name,
            "database_name": self.database_name,
            "user_name": self.user_name,
        }


@dataclass(frozen=True)
class MulticaBootstrapState:
    ready: bool
    phases_complete: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "phases_complete": list(self.phases_complete),
        }


@dataclass(frozen=True)
class MulticaHealthState:
    healthy: bool
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "checks": list(self.checks),
        }


@dataclass(frozen=True)
class MulticaManagedResource:
    resource_type: str
    resource_id: str
    resource_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class MulticaPhase:
    name: str
    ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class MulticaResult:
    outcome: str
    enabled: bool
    hostname: str | None
    api_hostname: str | None
    service: MulticaResourceRecord | None
    data: MulticaResourceRecord | None
    health: MulticaHealthState | None
    bootstrap: MulticaBootstrapState | None
    config: MulticaServiceConfig | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "enabled": self.enabled,
            "hostname": self.hostname,
            "api_hostname": self.api_hostname,
            "service": None if self.service is None else self.service.to_dict(),
            "data": None if self.data is None else self.data.to_dict(),
            "health": None if self.health is None else self.health.to_dict(),
            "bootstrap": None if self.bootstrap is None else self.bootstrap.to_dict(),
            "config": None if self.config is None else self.config.to_dict(),
            "notes": list(self.notes),
        }
