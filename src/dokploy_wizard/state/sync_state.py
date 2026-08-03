"""Strict desired, applied, and ownership records for the Shared Core synchronizer."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.state.sync_schema import (
    SYNC_CONTRACT_VERSION,
    JsonValue,
    ScheduleSpec,
    SyncStateError,
    canonical_digest,
    require_digest,
    require_exact_keys,
    require_string,
    require_uuid4,
)


@dataclass(frozen=True, slots=True)
class SyncDesiredState:
    """Hash-bound desired state for the model synchronizer."""

    owner_id: str
    config_sha256: str
    litellm_image_digest: str
    metadata_volume: str
    lock_path: str
    schedule_spec: ScheduleSpec
    catalog_id: str = "opencode-go"
    enabled: bool = True
    sync_contract_version: int = SYNC_CONTRACT_VERSION

    @classmethod
    def from_schedule(
        cls,
        *,
        owner_id: str,
        config_sha256: str,
        litellm_image_digest: str,
        metadata_volume: str,
        schedule_spec: ScheduleSpec,
    ) -> SyncDesiredState:
        return cls(
            owner_id=owner_id,
            config_sha256=config_sha256,
            litellm_image_digest=litellm_image_digest,
            metadata_volume=metadata_volume,
            lock_path="/var/lib/dokploy-wizard/opencode-go/sync.lock",
            schedule_spec=schedule_spec,
        )

    def __post_init__(self) -> None:
        require_uuid4(self.owner_id)
        require_digest(self.config_sha256, "config_sha256")
        image_repository, separator, digest = self.litellm_image_digest.partition("@sha256:")
        if image_repository == "" or separator == "" or "@" in image_repository:
            raise SyncStateError("litellm_image_digest must be a digest-pinned image.")
        require_digest(digest, "litellm_image_digest")
        if self.metadata_volume == "" or self.lock_path == "" or self.catalog_id != "opencode-go":
            raise SyncStateError("Shared Core sync desired state is invalid.")
        if type(self.enabled) is not bool or self.sync_contract_version != SYNC_CONTRACT_VERSION:
            raise SyncStateError("Unsupported sync desired-state contract.")

    @property
    def command_sha256(self) -> str:
        return self.schedule_spec.command_sha256

    @property
    def schedule_spec_sha256(self) -> str:
        return self.schedule_spec.fingerprint

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "catalog_id": self.catalog_id,
            "command_sha256": self.command_sha256,
            "config_sha256": self.config_sha256,
            "enabled": self.enabled,
            "litellm_image_digest": self.litellm_image_digest,
            "lock_path": self.lock_path,
            "metadata_volume": self.metadata_volume,
            "owner_id": self.owner_id,
            "schedule_spec": self.schedule_spec.to_dict(),
            "schedule_spec_sha256": self.schedule_spec_sha256,
            "sync_contract_version": self.sync_contract_version,
        }

    def fingerprint(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> SyncDesiredState:
        require_exact_keys(
            payload,
            frozenset(
                {
                    "catalog_id",
                    "command_sha256",
                    "config_sha256",
                    "enabled",
                    "litellm_image_digest",
                    "lock_path",
                    "metadata_volume",
                    "owner_id",
                    "schedule_spec",
                    "schedule_spec_sha256",
                    "sync_contract_version",
                }
            ),
            "sync desired state",
        )
        schedule_payload = payload["schedule_spec"]
        if not isinstance(schedule_payload, dict):
            raise SyncStateError("schedule_spec must be an object.")
        enabled = payload["enabled"]
        version = payload["sync_contract_version"]
        if type(enabled) is not bool or type(version) is not int:
            raise SyncStateError("sync desired state has invalid scalar types.")
        desired = cls(
            owner_id=require_string(payload, "owner_id"),
            config_sha256=require_string(payload, "config_sha256"),
            litellm_image_digest=require_string(payload, "litellm_image_digest"),
            metadata_volume=require_string(payload, "metadata_volume"),
            lock_path=require_string(payload, "lock_path"),
            schedule_spec=ScheduleSpec.from_dict(schedule_payload),
            catalog_id=require_string(payload, "catalog_id"),
            enabled=enabled,
            sync_contract_version=version,
        )
        if payload["command_sha256"] != desired.command_sha256 or (
            payload["schedule_spec_sha256"] != desired.schedule_spec_sha256
        ):
            raise SyncStateError("Sync desired state hashes do not match its exact specification.")
        return desired


@dataclass(frozen=True, slots=True)
class AppliedSyncState:
    """Applied state with explicit nulls until the first reconciliation completes."""

    desired_fingerprint: str
    previous_legacy_fingerprint: str | None
    dokploy_schedule_id: str | None
    compose_id: str | None
    catalog_resource_id: str | None
    model_set_sha256: str | None
    last_sync_receipt_sha256: str | None
    disable_tombstone_sha256: str | None
    sync_contract_version: int = SYNC_CONTRACT_VERSION

    @classmethod
    def initial(cls, desired: SyncDesiredState) -> AppliedSyncState:
        return cls(desired.fingerprint(), None, None, None, None, None, None, None)

    def __post_init__(self) -> None:
        require_digest(self.desired_fingerprint, "desired_fingerprint")
        for value in (
            self.previous_legacy_fingerprint,
            self.model_set_sha256,
            self.last_sync_receipt_sha256,
            self.disable_tombstone_sha256,
        ):
            if value is not None:
                require_digest(value, "applied hash")
        if self.sync_contract_version != SYNC_CONTRACT_VERSION:
            raise SyncStateError("Unsupported applied sync contract version.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "catalog_resource_id": self.catalog_resource_id,
            "compose_id": self.compose_id,
            "desired_fingerprint": self.desired_fingerprint,
            "disable_tombstone_sha256": self.disable_tombstone_sha256,
            "dokploy_schedule_id": self.dokploy_schedule_id,
            "last_sync_receipt_sha256": self.last_sync_receipt_sha256,
            "model_set_sha256": self.model_set_sha256,
            "previous_legacy_fingerprint": self.previous_legacy_fingerprint,
            "sync_contract_version": self.sync_contract_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> AppliedSyncState:
        expected = frozenset(
            {
                "catalog_resource_id",
                "compose_id",
                "desired_fingerprint",
                "disable_tombstone_sha256",
                "dokploy_schedule_id",
                "last_sync_receipt_sha256",
                "model_set_sha256",
                "previous_legacy_fingerprint",
                "sync_contract_version",
            }
        )
        require_exact_keys(payload, expected, "sync applied state")
        version = payload["sync_contract_version"]
        if type(version) is not int:
            raise SyncStateError("sync applied state version must be an integer.")
        return cls(
            desired_fingerprint=require_string(payload, "desired_fingerprint"),
            previous_legacy_fingerprint=_optional_string(payload, "previous_legacy_fingerprint"),
            dokploy_schedule_id=_optional_string(payload, "dokploy_schedule_id"),
            compose_id=_optional_string(payload, "compose_id"),
            catalog_resource_id=_optional_string(payload, "catalog_resource_id"),
            model_set_sha256=_optional_string(payload, "model_set_sha256"),
            last_sync_receipt_sha256=_optional_string(payload, "last_sync_receipt_sha256"),
            disable_tombstone_sha256=_optional_string(payload, "disable_tombstone_sha256"),
            sync_contract_version=version,
        )


def _optional_string(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload[key]
    if value is not None and (not isinstance(value, str) or value == ""):
        raise SyncStateError(f"{key} must be a non-empty string or null.")
    return value
