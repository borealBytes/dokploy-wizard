"""Typed models for the DocuSeal runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DocuSealManagedResource:
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
class DocuSealPostgresBinding:
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
class DocuSealServiceConfig:
    access_url: str
    postgres: DocuSealPostgresBinding

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_url": self.access_url,
            "postgres": self.postgres.to_dict(),
        }


@dataclass(frozen=True)
class DocuSealBootstrapState:
    initialized: bool | None
    secret_key_base_secret_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "initialized": self.initialized,
            "secret_key_base_secret_ref": self.secret_key_base_secret_ref,
        }


@dataclass(frozen=True)
class DocuSealHealthState:
    url: str
    path: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "path": self.path,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class DocuSealResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: DocuSealManagedResource | None
    persistent_data: DocuSealManagedResource | None
    bootstrap_state: DocuSealBootstrapState | None
    health_state: DocuSealHealthState | None
    config: DocuSealServiceConfig | None
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
            "bootstrap_state": None
            if self.bootstrap_state is None
            else self.bootstrap_state.to_dict(),
            "health_state": None if self.health_state is None else self.health_state.to_dict(),
            "config": None if self.config is None else self.config.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class DocuSealPhase:
    result: DocuSealResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class DocuSealResourceRecord:
    resource_id: str
    resource_name: str
