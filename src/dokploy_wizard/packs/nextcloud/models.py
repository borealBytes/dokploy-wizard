"""Typed models for the Nextcloud + OnlyOffice runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NextcloudManagedResource:
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
class NextcloudHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class NextcloudCommandCheck:
    command: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "passed": self.passed}


@dataclass(frozen=True)
class NextcloudPostgresBinding:
    database_name: str
    user_name: str
    password_secret_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "database_name": self.database_name,
            "password_secret_ref": self.password_secret_ref,
            "user_name": self.user_name,
        }


@dataclass(frozen=True)
class NextcloudRedisBinding:
    identity_name: str
    password_secret_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "identity_name": self.identity_name,
            "password_secret_ref": self.password_secret_ref,
        }


@dataclass(frozen=True)
class NextcloudServiceConfig:
    onlyoffice_url: str
    postgres: NextcloudPostgresBinding
    redis: NextcloudRedisBinding

    def to_dict(self) -> dict[str, Any]:
        return {
            "onlyoffice_url": self.onlyoffice_url,
            "postgres": self.postgres.to_dict(),
            "redis": self.redis.to_dict(),
        }


@dataclass(frozen=True)
class OnlyofficeServiceConfig:
    nextcloud_url: str
    integration_secret_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "integration_secret_ref": self.integration_secret_ref,
            "nextcloud_url": self.nextcloud_url,
        }


@dataclass(frozen=True)
class NextcloudServiceRuntime:
    hostname: str
    url: str
    service: NextcloudManagedResource
    data_volume: NextcloudManagedResource
    health_check: NextcloudHealthCheck
    config: NextcloudServiceConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "data_volume": self.data_volume.to_dict(),
            "health_check": self.health_check.to_dict(),
            "hostname": self.hostname,
            "service": self.service.to_dict(),
            "url": self.url,
        }


@dataclass(frozen=True)
class OnlyofficeServiceRuntime:
    hostname: str
    url: str
    service: NextcloudManagedResource
    data_volume: NextcloudManagedResource
    health_check: NextcloudHealthCheck
    config: OnlyofficeServiceConfig
    document_server_check: NextcloudCommandCheck

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "data_volume": self.data_volume.to_dict(),
            "document_server_check": self.document_server_check.to_dict(),
            "health_check": self.health_check.to_dict(),
            "hostname": self.hostname,
            "service": self.service.to_dict(),
            "url": self.url,
        }


@dataclass(frozen=True)
class TalkRuntime:
    app_id: str
    enabled: bool | None
    enabled_check: NextcloudCommandCheck
    signaling_check: NextcloudCommandCheck
    stun_check: NextcloudCommandCheck
    turn_check: NextcloudCommandCheck

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "enabled": self.enabled,
            "enabled_check": self.enabled_check.to_dict(),
            "signaling_check": self.signaling_check.to_dict(),
            "stun_check": self.stun_check.to_dict(),
            "turn_check": self.turn_check.to_dict(),
        }


@dataclass(frozen=True)
class NextcloudBundleVerification:
    onlyoffice_document_server_check: NextcloudCommandCheck
    talk: TalkRuntime


@dataclass(frozen=True)
class NextcloudResult:
    outcome: str
    enabled: bool
    nextcloud: NextcloudServiceRuntime | None
    onlyoffice: OnlyofficeServiceRuntime | None
    talk: TalkRuntime | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "nextcloud": None if self.nextcloud is None else self.nextcloud.to_dict(),
            "notes": list(self.notes),
            "onlyoffice": None if self.onlyoffice is None else self.onlyoffice.to_dict(),
            "outcome": self.outcome,
            "talk": None if self.talk is None else self.talk.to_dict(),
        }


@dataclass(frozen=True)
class NextcloudPhase:
    result: NextcloudResult
    nextcloud_service_resource_id: str | None
    onlyoffice_service_resource_id: str | None
    nextcloud_volume_resource_id: str | None
    onlyoffice_volume_resource_id: str | None


@dataclass(frozen=True)
class NextcloudResourceRecord:
    resource_id: str
    resource_name: str
