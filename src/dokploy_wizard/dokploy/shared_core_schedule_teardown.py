"""Durable authorization and outcome receipt for sync schedule teardown."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    canonical_digest,
    require_digest,
    require_exact_keys,
    require_string,
    require_uuid4,
)


@dataclass(frozen=True, slots=True)
class ScheduleTeardownReceipt:
    owner_id: str
    action: str
    schedule_id: str
    compose_id: str
    authorization_receipt_sha256: str
    before_sha256: str
    after_sha256: str | None
    completed_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        if self.schema_version != 1 or self.action not in {"deleted", "restored"}:
            raise SyncStateError("Schedule teardown receipt action or schema is invalid.")
        if self.schedule_id == "" or self.compose_id == "" or self.completed_at == "":
            raise SyncStateError("Schedule teardown receipt identity is invalid.")
        require_digest(
            self.authorization_receipt_sha256,
            "authorization_receipt_sha256",
        )
        require_digest(self.before_sha256, "before_sha256")
        if self.action == "deleted" and self.after_sha256 is not None:
            raise SyncStateError("Deleted schedule teardown must record an absent result.")
        if self.action == "restored" and self.after_sha256 is None:
            raise SyncStateError("Restored schedule teardown must bind the restored result.")
        if self.after_sha256 is not None:
            require_digest(self.after_sha256, "after_sha256")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action": self.action,
            "after_sha256": self.after_sha256,
            "authorization_receipt_sha256": self.authorization_receipt_sha256,
            "before_sha256": self.before_sha256,
            "completed_at": self.completed_at,
            "compose_id": self.compose_id,
            "owner_id": self.owner_id,
            "schedule_id": self.schedule_id,
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> ScheduleTeardownReceipt:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "teardown receipt")
        after = payload["after_sha256"]
        if after is not None and not isinstance(after, str):
            raise SyncStateError("Schedule teardown after_sha256 is malformed.")
        schema = payload["schema_version"]
        if type(schema) is not int:
            raise SyncStateError("Schedule teardown schema_version is malformed.")
        return cls(
            owner_id=require_string(payload, "owner_id"),
            action=require_string(payload, "action"),
            schedule_id=require_string(payload, "schedule_id"),
            compose_id=require_string(payload, "compose_id"),
            authorization_receipt_sha256=require_string(
                payload,
                "authorization_receipt_sha256",
            ),
            before_sha256=require_string(payload, "before_sha256"),
            after_sha256=after,
            completed_at=require_string(payload, "completed_at"),
            schema_version=schema,
        )
