"""Ownership provenance contract for the Shared Core synchronizer schedule."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    require_digest,
    require_exact_keys,
    require_string,
    require_uuid4,
)


@dataclass(frozen=True, slots=True)
class SyncOwnershipMetadata:
    """Provenance and deletion authority for the owned schedule resource."""

    owner_id: str
    action_provenance: str
    remote_fingerprint: str
    spec_hash: str
    physical_target_id: str
    creation_receipt_sha256: str | None
    preimage_receipt_sha256: str | None
    deletion_policy: str
    metadata_version: int = 2

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        require_digest(self.remote_fingerprint, "remote_fingerprint")
        require_digest(self.spec_hash, "spec_hash")
        if self.physical_target_id == "" or self.metadata_version != 2:
            raise SyncStateError("Sync ownership metadata is invalid.")
        for value in (self.creation_receipt_sha256, self.preimage_receipt_sha256):
            if value is not None:
                require_digest(value, "ownership receipt")
        expected_policy = {
            "created": "delete",
            "updated": "restore",
            "reused": "preserve",
            "legacy_unproven": "preserve",
        }.get(self.action_provenance)
        if expected_policy != self.deletion_policy:
            raise SyncStateError("Sync ownership provenance and deletion policy disagree.")
        if self.action_provenance == "created" and (
            self.creation_receipt_sha256 is None or self.preimage_receipt_sha256 is not None
        ):
            raise SyncStateError("Created sync resources require creation evidence only.")
        if self.action_provenance == "updated" and self.preimage_receipt_sha256 is None:
            raise SyncStateError("Updated sync resources require a preimage receipt.")
        if self.action_provenance in {"reused", "legacy_unproven"} and (
            self.creation_receipt_sha256 is not None
            or self.preimage_receipt_sha256 is not None
        ):
            raise SyncStateError("preserve provenance forbids destructive receipts.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_provenance": self.action_provenance,
            "creation_receipt_sha256": self.creation_receipt_sha256,
            "deletion_policy": self.deletion_policy,
            "metadata_version": self.metadata_version,
            "owner_id": self.owner_id,
            "physical_target_id": self.physical_target_id,
            "preimage_receipt_sha256": self.preimage_receipt_sha256,
            "remote_fingerprint": self.remote_fingerprint,
            "spec_hash": self.spec_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> SyncOwnershipMetadata:
        require_exact_keys(
            payload,
            frozenset(cls.__dataclass_fields__),
            "sync ownership metadata",
        )
        version = payload["metadata_version"]
        if type(version) is not int:
            raise SyncStateError("ownership metadata version must be an integer.")
        return cls(
            owner_id=require_string(payload, "owner_id"),
            action_provenance=require_string(payload, "action_provenance"),
            remote_fingerprint=require_string(payload, "remote_fingerprint"),
            spec_hash=require_string(payload, "spec_hash"),
            physical_target_id=require_string(payload, "physical_target_id"),
            creation_receipt_sha256=_optional_string(payload, "creation_receipt_sha256"),
            preimage_receipt_sha256=_optional_string(payload, "preimage_receipt_sha256"),
            deletion_policy=require_string(payload, "deletion_policy"),
            metadata_version=version,
        )


def _optional_string(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload[key]
    if value is not None and (not isinstance(value, str) or value == ""):
        raise SyncStateError(f"{key} must be a non-empty string or null.")
    return value
