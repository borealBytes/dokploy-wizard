"""Typed desired-state models for the SurfSense pack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SurfSensePostgresBinding:
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
class SurfSenseRedisBinding:
    url_secret_ref: str

    def to_dict(self) -> dict[str, str]:
        return {"url_secret_ref": self.url_secret_ref}


@dataclass(frozen=True)
class SurfSenseServiceEndpoints:
    frontend_url: str
    api_url: str
    zero_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "frontend_url": self.frontend_url,
            "api_url": self.api_url,
            "zero_url": self.zero_url,
        }


@dataclass(frozen=True)
class SurfSenseManagedResource:
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
class SurfSenseHealthCheck:
    url: str
    path: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "path": self.path, "url": self.url}


@dataclass(frozen=True)
class SurfSenseBootstrapState:
    created: bool | None
    verified_existing: bool | None

    def to_dict(self) -> dict[str, bool | None]:
        return {
            "created": self.created,
            "verified_existing": self.verified_existing,
        }


@dataclass(frozen=True)
class SurfSenseServiceConfig:
    endpoints: SurfSenseServiceEndpoints
    postgres: SurfSensePostgresBinding
    redis: SurfSenseRedisBinding

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": self.endpoints.to_dict(),
            "postgres": self.postgres.to_dict(),
            "redis": self.redis.to_dict(),
        }


@dataclass(frozen=True)
class SurfSenseResult:
    outcome: str
    enabled: bool
    frontend_hostname: str | None
    api_hostname: str | None
    zero_hostname: str | None
    service: SurfSenseManagedResource | None
    persistent_data: SurfSenseManagedResource | None
    bootstrap_state: SurfSenseBootstrapState | None
    health_check: SurfSenseHealthCheck | None
    config: SurfSenseServiceConfig | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_hostname": self.api_hostname,
            "bootstrap_state": None
            if self.bootstrap_state is None
            else self.bootstrap_state.to_dict(),
            "config": None if self.config is None else self.config.to_dict(),
            "enabled": self.enabled,
            "frontend_hostname": self.frontend_hostname,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "notes": list(self.notes),
            "outcome": self.outcome,
            "persistent_data": None
            if self.persistent_data is None
            else self.persistent_data.to_dict(),
            "service": None if self.service is None else self.service.to_dict(),
            "zero_hostname": self.zero_hostname,
        }


@dataclass(frozen=True)
class SurfSensePhase:
    result: SurfSenseResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class SurfSenseResourceRecord:
    resource_id: str
    resource_name: str
