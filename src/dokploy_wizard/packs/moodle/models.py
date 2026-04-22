"""Typed models for the Moodle runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MoodleManagedResource:
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
class MoodleHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class MoodlePostgresBinding:
    database_name: str
    user_name: str
    password_secret_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "database_name": self.database_name,
            "user_name": self.user_name,
            "password_secret_ref": self.password_secret_ref,
        }


@dataclass(frozen=True)
class MoodleServiceConfig:
    access_url: str
    postgres: MoodlePostgresBinding

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_url": self.access_url,
            "postgres": self.postgres.to_dict(),
        }


@dataclass(frozen=True)
class MoodleResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: MoodleManagedResource | None
    persistent_data: MoodleManagedResource | None
    health_check: MoodleHealthCheck | None
    config: MoodleServiceConfig | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "enabled": self.enabled,
            "hostname": self.hostname,
            "service": None if self.service is None else self.service.to_dict(),
            "persistent_data": None
            if self.persistent_data is None
            else self.persistent_data.to_dict(),
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "config": None if self.config is None else self.config.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MoodlePhase:
    result: MoodleResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class MoodleResourceRecord:
    resource_id: str
    resource_name: str
