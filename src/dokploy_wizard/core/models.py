"""Typed shared-core planning and reconciliation models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_FORBIDDEN_POSTGRES_USERS = {"admin", "postgres", "root"}
_FORBIDDEN_REDIS_IDENTITIES = {"admin", "default", "root"}


def _ensure_non_empty(value: str, field_name: str) -> str:
    if value == "":
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


@dataclass(frozen=True)
class SharedPostgresServicePlan:
    service_name: str

    def __post_init__(self) -> None:
        _ensure_non_empty(self.service_name, "service_name")

    def to_dict(self) -> dict[str, str]:
        return {"service_name": self.service_name}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SharedPostgresServicePlan:
        service_name = payload.get("service_name")
        if not isinstance(service_name, str):
            raise ValueError("SharedPostgresServicePlan.service_name must be a string.")
        return cls(service_name=service_name)


@dataclass(frozen=True)
class SharedRedisServicePlan:
    service_name: str

    def __post_init__(self) -> None:
        _ensure_non_empty(self.service_name, "service_name")

    def to_dict(self) -> dict[str, str]:
        return {"service_name": self.service_name}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SharedRedisServicePlan:
        service_name = payload.get("service_name")
        if not isinstance(service_name, str):
            raise ValueError("SharedRedisServicePlan.service_name must be a string.")
        return cls(service_name=service_name)


@dataclass(frozen=True)
class SharedPostgresAllocation:
    database_name: str
    user_name: str
    password_secret_ref: str

    def __post_init__(self) -> None:
        _ensure_non_empty(self.database_name, "database_name")
        _ensure_non_empty(self.user_name, "user_name")
        _ensure_non_empty(self.password_secret_ref, "password_secret_ref")
        if self.user_name.lower() in _FORBIDDEN_POSTGRES_USERS:
            raise ValueError(
                "SharedPostgresAllocation.user_name cannot use admin/root credentials."
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "database_name": self.database_name,
            "password_secret_ref": self.password_secret_ref,
            "user_name": self.user_name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SharedPostgresAllocation:
        database_name = payload.get("database_name")
        user_name = payload.get("user_name")
        password_secret_ref = payload.get("password_secret_ref")
        if not isinstance(database_name, str):
            raise ValueError("SharedPostgresAllocation.database_name must be a string.")
        if not isinstance(user_name, str):
            raise ValueError("SharedPostgresAllocation.user_name must be a string.")
        if not isinstance(password_secret_ref, str):
            raise ValueError("SharedPostgresAllocation.password_secret_ref must be a string.")
        return cls(
            database_name=database_name,
            user_name=user_name,
            password_secret_ref=password_secret_ref,
        )


@dataclass(frozen=True)
class SharedRedisAllocation:
    identity_name: str
    password_secret_ref: str

    def __post_init__(self) -> None:
        _ensure_non_empty(self.identity_name, "identity_name")
        _ensure_non_empty(self.password_secret_ref, "password_secret_ref")
        if self.identity_name.lower() in _FORBIDDEN_REDIS_IDENTITIES:
            raise ValueError(
                "SharedRedisAllocation.identity_name cannot use admin/root identities."
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "identity_name": self.identity_name,
            "password_secret_ref": self.password_secret_ref,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SharedRedisAllocation:
        identity_name = payload.get("identity_name")
        password_secret_ref = payload.get("password_secret_ref")
        if not isinstance(identity_name, str):
            raise ValueError("SharedRedisAllocation.identity_name must be a string.")
        if not isinstance(password_secret_ref, str):
            raise ValueError("SharedRedisAllocation.password_secret_ref must be a string.")
        return cls(
            identity_name=identity_name,
            password_secret_ref=password_secret_ref,
        )


@dataclass(frozen=True)
class PackSharedAllocation:
    pack_name: str
    network_alias: str
    postgres: SharedPostgresAllocation | None
    redis: SharedRedisAllocation | None

    def __post_init__(self) -> None:
        _ensure_non_empty(self.pack_name, "pack_name")
        _ensure_non_empty(self.network_alias, "network_alias")

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_alias": self.network_alias,
            "pack_name": self.pack_name,
            "postgres": None if self.postgres is None else self.postgres.to_dict(),
            "redis": None if self.redis is None else self.redis.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PackSharedAllocation:
        pack_name = payload.get("pack_name")
        network_alias = payload.get("network_alias")
        postgres_payload = payload.get("postgres")
        redis_payload = payload.get("redis")
        if not isinstance(pack_name, str):
            raise ValueError("PackSharedAllocation.pack_name must be a string.")
        if not isinstance(network_alias, str):
            raise ValueError("PackSharedAllocation.network_alias must be a string.")
        if postgres_payload is not None and not isinstance(postgres_payload, dict):
            raise ValueError("PackSharedAllocation.postgres must be an object or null.")
        if redis_payload is not None and not isinstance(redis_payload, dict):
            raise ValueError("PackSharedAllocation.redis must be an object or null.")
        return cls(
            pack_name=pack_name,
            network_alias=network_alias,
            postgres=(
                None
                if postgres_payload is None
                else SharedPostgresAllocation.from_dict(postgres_payload)
            ),
            redis=(
                None if redis_payload is None else SharedRedisAllocation.from_dict(redis_payload)
            ),
        )


@dataclass(frozen=True)
class SharedCorePlan:
    network_name: str
    postgres: SharedPostgresServicePlan | None
    redis: SharedRedisServicePlan | None
    allocations: tuple[PackSharedAllocation, ...]

    def __post_init__(self) -> None:
        _ensure_non_empty(self.network_name, "network_name")
        pack_names = tuple(allocation.pack_name for allocation in self.allocations)
        if tuple(sorted(pack_names)) != pack_names:
            raise ValueError("SharedCorePlan allocations must be sorted by pack_name.")

    def requires_reconciliation(self) -> bool:
        return bool(self.allocations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "network_name": self.network_name,
            "postgres": None if self.postgres is None else self.postgres.to_dict(),
            "redis": None if self.redis is None else self.redis.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SharedCorePlan:
        network_name = payload.get("network_name")
        postgres_payload = payload.get("postgres")
        redis_payload = payload.get("redis")
        allocations_payload = payload.get("allocations")
        if not isinstance(network_name, str):
            raise ValueError("SharedCorePlan.network_name must be a string.")
        if postgres_payload is not None and not isinstance(postgres_payload, dict):
            raise ValueError("SharedCorePlan.postgres must be an object or null.")
        if redis_payload is not None and not isinstance(redis_payload, dict):
            raise ValueError("SharedCorePlan.redis must be an object or null.")
        if not isinstance(allocations_payload, list):
            raise ValueError("SharedCorePlan.allocations must be a list.")
        allocations: list[PackSharedAllocation] = []
        for item in allocations_payload:
            if not isinstance(item, dict):
                raise ValueError("SharedCorePlan allocation entries must be objects.")
            allocations.append(PackSharedAllocation.from_dict(item))
        return cls(
            network_name=network_name,
            postgres=(
                None
                if postgres_payload is None
                else SharedPostgresServicePlan.from_dict(postgres_payload)
            ),
            redis=(
                None if redis_payload is None else SharedRedisServicePlan.from_dict(redis_payload)
            ),
            allocations=tuple(allocations),
        )


@dataclass(frozen=True)
class SharedCoreManagedResource:
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
class SharedCoreResult:
    outcome: str
    network: SharedCoreManagedResource | None
    postgres: SharedCoreManagedResource | None
    redis: SharedCoreManagedResource | None
    allocations: tuple[PackSharedAllocation, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "network": None if self.network is None else self.network.to_dict(),
            "notes": list(self.notes),
            "outcome": self.outcome,
            "postgres": None if self.postgres is None else self.postgres.to_dict(),
            "redis": None if self.redis is None else self.redis.to_dict(),
        }


@dataclass(frozen=True)
class SharedCorePhase:
    result: SharedCoreResult
    network_resource_id: str | None
    postgres_resource_id: str | None
    redis_resource_id: str | None


@dataclass(frozen=True)
class SharedCoreResourceRecord:
    resource_id: str
    resource_name: str
