"""Typed models for the Paperclip runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dokploy_wizard.core.models import SharedPostgresAllocation


@dataclass(frozen=True)
class PaperclipResourceRecord:
    resource_id: str
    resource_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class PaperclipServiceConfig:
    hostname: str
    postgres_service_name: str
    postgres: SharedPostgresAllocation

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "postgres_service_name": self.postgres_service_name,
            "postgres": self.postgres.to_dict(),
        }


@dataclass(frozen=True)
class PaperclipBootstrapState:
    ready: bool

    def to_dict(self) -> dict[str, bool]:
        return {"ready": self.ready}


@dataclass(frozen=True)
class PaperclipHealthState:
    healthy: bool

    def to_dict(self) -> dict[str, bool]:
        return {"healthy": self.healthy}


@dataclass(frozen=True)
class PaperclipResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: PaperclipResourceRecord | None
    health: PaperclipHealthState | None
    bootstrap: PaperclipBootstrapState | None
    config: PaperclipServiceConfig | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "enabled": self.enabled,
            "hostname": self.hostname,
            "service": None if self.service is None else self.service.to_dict(),
            "health": None if self.health is None else self.health.to_dict(),
            "bootstrap": None if self.bootstrap is None else self.bootstrap.to_dict(),
            "config": None if self.config is None else self.config.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PaperclipPhase:
    result: PaperclipResult
    service_resource_id: str | None
    data_resource_id: str | None
