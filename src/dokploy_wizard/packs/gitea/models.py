"""Typed models for the Gitea runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GiteaManagedResource:
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
class GiteaPostgresBinding:
    """The shared-core Postgres allocation Gitea runs on."""

    database_name: str
    user_name: str
    password_secret_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_name": self.database_name,
            "user_name": self.user_name,
            "password_secret_ref": self.password_secret_ref,
        }


@dataclass(frozen=True)
class GiteaStorageBinding:
    """The SeaweedFS bucket Gitea stores LFS objects and attachments in."""

    bucket: str
    region: str
    endpoint_url: str
    access_key_secret_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "access_key_secret_ref": self.access_key_secret_ref,
        }


@dataclass(frozen=True)
class GiteaBootstrapState:
    initialized: bool | None
    secret_key_secret_ref: str
    bucket_ready: bool | None
    provisioner_ready: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initialized": self.initialized,
            "secret_key_secret_ref": self.secret_key_secret_ref,
            "bucket_ready": self.bucket_ready,
            "provisioner_ready": self.provisioner_ready,
        }


@dataclass(frozen=True)
class GiteaHealthState:
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
class GiteaResult:
    outcome: str
    enabled: bool
    hostname: str | None
    service: GiteaManagedResource | None
    persistent_data: GiteaManagedResource | None
    bootstrap_state: GiteaBootstrapState | None
    health_state: GiteaHealthState | None
    postgres: GiteaPostgresBinding | None
    storage: GiteaStorageBinding | None
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
            "postgres": None if self.postgres is None else self.postgres.to_dict(),
            "storage": None if self.storage is None else self.storage.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class GiteaPhase:
    result: GiteaResult
    service_resource_id: str | None
    data_resource_id: str | None


@dataclass(frozen=True)
class GiteaResourceRecord:
    resource_id: str
    resource_name: str
