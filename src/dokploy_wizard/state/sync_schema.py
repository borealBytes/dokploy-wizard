"""Shared Core synchronizer primitives that do not touch persistent storage."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, TypeAlias

SYNC_CONTRACT_VERSION: Final = 2
CRON_EXPRESSION: Final = "0 3 * * *"
TIMEZONE: Final = "UTC"
SYNC_COMMAND: Final = (
    "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={owner_id} TZ=UTC python "
    "/opt/dokploy-wizard/opencode_go_sync.py --config /opt/dokploy-wizard/opencode-go.json "
    "--state-dir /var/lib/dokploy-wizard/opencode-go --lock-file "
    "/var/lib/dokploy-wizard/opencode-go/sync.lock --once --max-runtime-seconds 300 "
    "--http-timeout-seconds 30"
)
JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class SyncStateError(RuntimeError):
    """Raised when the model-sync state contract is missing or inconsistent."""


def sha256_text(value: str) -> str:
    """Return the canonical text digest used by sync contracts."""

    return sha256(value.encode("utf-8")).hexdigest()


def canonical_digest(value: dict[str, JsonValue]) -> str:
    """Return a hash for a canonical JSON object."""

    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def require_digest(value: str, field_name: str) -> None:
    """Require a lowercase SHA-256 hex digest."""

    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SyncStateError(f"{field_name} must be a lowercase SHA-256 digest.")


def require_uuid4(value: str) -> None:
    """Require the owner identifier to be a canonical UUID4."""

    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise SyncStateError("owner_id must be a UUID4.") from error
    if parsed.version != 4 or str(parsed) != value:
        raise SyncStateError("owner_id must be a UUID4.")


def require_exact_keys(
    payload: dict[str, JsonValue], expected: frozenset[str], label: str
) -> None:
    """Reject schema aliases and unknown fields at the persistence boundary."""

    if set(payload) != expected:
        raise SyncStateError(f"{label} must contain the exact keys {sorted(expected)}.")


def require_string(payload: dict[str, JsonValue], key: str) -> str:
    """Read a required non-empty string from a strict JSON object."""

    value = payload[key]
    if not isinstance(value, str) or value == "":
        raise SyncStateError(f"{key} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Exact Dokploy schedule projection shared by immediate and scheduled execution."""

    name: str
    compose_id: str
    service_name: str
    command: str
    schedule_type: str = "compose"
    shell_type: str = "bash"
    enabled: bool = True
    cron_expression: str = CRON_EXPRESSION
    timezone: str = TIMEZONE

    @classmethod
    def for_shared_core(cls, *, stack_name: str, compose_id: str, owner_id: str) -> ScheduleSpec:
        """Build the only permitted schedule specification for a stack."""

        require_uuid4(owner_id)
        if stack_name == "" or compose_id == "":
            raise SyncStateError("stack_name and compose_id must be non-empty.")
        return cls(
            name=f"{stack_name}-shared-litellm-opencode-go-sync",
            compose_id=compose_id,
            service_name=f"{stack_name}-shared-litellm",
            command=SYNC_COMMAND.format(owner_id=owner_id),
        )

    def __post_init__(self) -> None:
        owner_marker = self.command.split(" ", 1)[0]
        owner_prefix = "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID="
        owner_id = owner_marker.removeprefix(owner_prefix)
        canonical_command = SYNC_COMMAND.format(owner_id=owner_id)
        if (
            self.name == ""
            or self.compose_id == ""
            or self.service_name == ""
            or self.command == ""
            or self.schedule_type != "compose"
            or self.shell_type != "bash"
            or self.enabled is not True
            or self.cron_expression != CRON_EXPRESSION
            or self.timezone != TIMEZONE
            or not owner_marker.startswith(owner_prefix)
            or self.command != canonical_command
        ):
            raise SyncStateError("Shared Core sync schedule does not match the fixed contract.")
        require_uuid4(owner_id)

    @property
    def command_sha256(self) -> str:
        """Return the command binding used by desired state."""

        return sha256_text(self.command)

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize the complete Dokploy schedule shape."""

        return {
            "command": self.command,
            "compose_id": self.compose_id,
            "cron_expression": self.cron_expression,
            "enabled": self.enabled,
            "name": self.name,
            "schedule_type": self.schedule_type,
            "service_name": self.service_name,
            "shell_type": self.shell_type,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> ScheduleSpec:
        """Parse an exact persisted schedule without accepting aliases."""

        require_exact_keys(
            payload,
            frozenset(
                {
                    "command",
                    "compose_id",
                    "cron_expression",
                    "enabled",
                    "name",
                    "schedule_type",
                    "service_name",
                    "shell_type",
                    "timezone",
                }
            ),
            "schedule_spec",
        )
        enabled = payload["enabled"]
        if type(enabled) is not bool:
            raise SyncStateError("schedule_spec.enabled must be a boolean.")
        return cls(
            command=require_string(payload, "command"),
            compose_id=require_string(payload, "compose_id"),
            cron_expression=require_string(payload, "cron_expression"),
            enabled=enabled,
            name=require_string(payload, "name"),
            schedule_type=require_string(payload, "schedule_type"),
            service_name=require_string(payload, "service_name"),
            shell_type=require_string(payload, "shell_type"),
            timezone=require_string(payload, "timezone"),
        )

    @property
    def fingerprint(self) -> str:
        """Return the canonical schedule specification digest."""

        return canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class SyncOwner:
    """Durable one-time owner identity for schedule ownership."""

    owner_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        if self.schema_version != 1:
            raise SyncStateError("Unsupported sync owner schema version.")

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize the owner document exactly."""

        return {"owner_id": self.owner_id, "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> SyncOwner:
        """Parse an exact owner document."""

        require_exact_keys(payload, frozenset({"owner_id", "schema_version"}), "sync owner")
        schema_version = payload["schema_version"]
        if type(schema_version) is not int:
            raise SyncStateError("sync owner schema_version must be an integer.")
        return cls(owner_id=require_string(payload, "owner_id"), schema_version=schema_version)
