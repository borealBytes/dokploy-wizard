"""Typed models for the FileX runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FilexManagedResource:
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
class FilexStorageBinding:
    """The SeaweedFS bucket FileX is seeded with as its default storage."""

    bucket: str
    prefix: str
    region: str
    endpoint_url: str
    access_key_secret_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "access_key_secret_ref": self.access_key_secret_ref,
        }


@dataclass(frozen=True)
class FilexBootstrapState:
    initialized: bool | None
    secret_key_secret_ref: str
    bucket_ready: bool | None
    versioning_enabled: bool | None
    login_verified: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initialized": self.initialized,
            "secret_key_secret_ref": self.secret_key_secret_ref,
            "bucket_ready": self.bucket_ready,
            "versioning_enabled": self.versioning_enabled,
            "login_verified": self.login_verified,
        }


@dataclass(frozen=True)
class FilexHealthState:
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
class FilexResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: FilexManagedResource | None
    persistent_data: FilexManagedResource | None
    bootstrap_state: FilexBootstrapState | None
    health_state: FilexHealthState | None
    storage: FilexStorageBinding | None
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
            "storage": None if self.storage is None else self.storage.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FilexPhase:
    result: FilexResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class FilexResourceRecord:
    resource_id: str
    resource_name: str
