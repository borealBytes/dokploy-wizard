"""Canonical mutation receipts for the owner-bound Dokploy sync schedule."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    canonical_digest,
    require_digest,
    require_exact_keys,
    require_string,
    require_uuid4,
)


def schedule_record_payload(record: DokployScheduleRecord) -> dict[str, JsonValue]:
    if record.compose_id is None:
        raise SyncStateError("Schedule record does not bind its compose id.")
    return {
        "command": record.command,
        "compose_id": record.compose_id,
        "cron_expression": record.cron_expression,
        "enabled": record.enabled,
        "name": record.name,
        "schedule_id": record.schedule_id,
        "schedule_type": record.schedule_type,
        "service_name": record.service_name,
        "shell_type": record.shell_type,
        "timezone": record.timezone,
    }


def schedule_record_fingerprint(record: DokployScheduleRecord) -> str:
    return canonical_digest(schedule_record_payload(record))


@dataclass(frozen=True, slots=True)
class ScheduleMutationReceipt:
    owner_id: str
    action: str
    desired_fingerprint: str
    before_spec_sha256: str | None
    remote: DokployScheduleRecord
    preimage: DokployScheduleRecord | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        require_digest(self.desired_fingerprint, "desired_fingerprint")
        if self.schema_version != 1 or self.action not in {"created", "updated", "reused"}:
            raise SyncStateError("Schedule mutation receipt action or schema is invalid.")
        if self.action == "updated" and (
            self.before_spec_sha256 is None or self.preimage is None
        ):
            raise SyncStateError("Updated schedule receipts require a full preimage and hash.")
        if self.action != "updated" and (
            self.before_spec_sha256 is not None or self.preimage is not None
        ):
            raise SyncStateError("Only updated schedule receipts may bind a preimage.")
        if self.before_spec_sha256 is not None:
            require_digest(self.before_spec_sha256, "before_spec_sha256")
            preimage = self.preimage
            if preimage is None or schedule_record_fingerprint(preimage) != self.before_spec_sha256:
                raise SyncStateError("Schedule receipt preimage does not match its hash.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action": self.action,
            "before_spec_sha256": self.before_spec_sha256,
            "desired_fingerprint": self.desired_fingerprint,
            "owner_id": self.owner_id,
            "preimage": (
                None if self.preimage is None else schedule_record_payload(self.preimage)
            ),
            "remote": schedule_record_payload(self.remote),
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> ScheduleMutationReceipt:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "schedule receipt")
        remote = payload["remote"]
        preimage = payload["preimage"]
        if not isinstance(remote, dict):
            raise SyncStateError("Schedule receipt remote must be an object.")
        if preimage is not None and not isinstance(preimage, dict):
            raise SyncStateError("Schedule receipt preimage must be an object or null.")
        return cls(
            owner_id=require_string(payload, "owner_id"),
            action=require_string(payload, "action"),
            desired_fingerprint=require_string(payload, "desired_fingerprint"),
            before_spec_sha256=_optional_string(payload, "before_spec_sha256"),
            remote=_schedule_from_payload(remote),
            preimage=None if preimage is None else _schedule_from_payload(preimage),
            schema_version=_int(payload, "schema_version"),
        )


@dataclass(frozen=True, slots=True)
class DisableTombstone:
    owner_id: str
    schedule_id: str
    desired_fingerprint: str
    remote_fingerprint: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        if self.schema_version != 1 or self.schedule_id == "":
            raise SyncStateError("Disable tombstone identity or schema is invalid.")
        require_digest(self.desired_fingerprint, "desired_fingerprint")
        require_digest(self.remote_fingerprint, "remote_fingerprint")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "desired_fingerprint": self.desired_fingerprint,
            "owner_id": self.owner_id,
            "remote_fingerprint": self.remote_fingerprint,
            "schedule_id": self.schedule_id,
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> DisableTombstone:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "disable tombstone")
        return cls(
            owner_id=require_string(payload, "owner_id"),
            schedule_id=require_string(payload, "schedule_id"),
            desired_fingerprint=require_string(payload, "desired_fingerprint"),
            remote_fingerprint=require_string(payload, "remote_fingerprint"),
            schema_version=_int(payload, "schema_version"),
        )


def _schedule_from_payload(payload: dict[str, JsonValue]) -> DokployScheduleRecord:
    require_exact_keys(
        payload,
        frozenset(
            {
                "command",
                "compose_id",
                "cron_expression",
                "enabled",
                "name",
                "schedule_id",
                "schedule_type",
                "service_name",
                "shell_type",
                "timezone",
            }
        ),
        "schedule receipt remote",
    )
    enabled = payload["enabled"]
    if type(enabled) is not bool:
        raise SyncStateError("Schedule receipt enabled must be a boolean.")
    return DokployScheduleRecord(
        schedule_id=require_string(payload, "schedule_id"),
        name=require_string(payload, "name"),
        service_name=require_string(payload, "service_name"),
        cron_expression=require_string(payload, "cron_expression"),
        timezone=require_string(payload, "timezone"),
        shell_type=require_string(payload, "shell_type"),
        command=require_string(payload, "command"),
        enabled=enabled,
        compose_id=require_string(payload, "compose_id"),
        schedule_type=require_string(payload, "schedule_type"),
    )


def _optional_string(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload[key]
    if value is not None and (not isinstance(value, str) or value == ""):
        raise SyncStateError(f"{key} must be a non-empty string or null.")
    return value


def _int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise SyncStateError(f"{key} must be an integer.")
    return value
