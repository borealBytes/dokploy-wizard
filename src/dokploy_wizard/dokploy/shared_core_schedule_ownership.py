"""Ownership validation and metadata for Shared Core schedules."""

from __future__ import annotations

import uuid

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.state.shared_core_sync import (
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
)

_OWNER_PREFIX = "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID="


def require_owner(record: DokployScheduleRecord, expected_owner_id: str) -> None:
    command_tokens = record.command.split(" ", 1)
    if len(command_tokens) != 2 or not command_tokens[0].startswith(_OWNER_PREFIX):
        raise SyncStateError("Same-name schedule has no valid owner marker.")
    owner_id = command_tokens[0].removeprefix(_OWNER_PREFIX)
    try:
        parsed = uuid.UUID(owner_id)
    except ValueError as error:
        raise SyncStateError("Same-name schedule has a malformed owner marker.") from error
    if parsed.version != 4 or owner_id != expected_owner_id:
        raise SyncStateError("Same-name schedule belongs to a different owner.")


def ownership_metadata(
    desired: SyncDesiredState,
    remote: DokployScheduleRecord,
    receipt: ScheduleMutationReceipt,
    existing: SyncOwnershipMetadata | None,
) -> SyncOwnershipMetadata:
    if existing is not None:
        owner_matches = existing.owner_id == desired.owner_id
        target_matches = existing.physical_target_id == remote.schedule_id
        if not owner_matches or not target_matches:
            raise SyncStateError(
                "Existing schedule ownership metadata does not match remote state."
            )
        if receipt.action == "reused" and existing.spec_hash != desired.schedule_spec_sha256:
            raise SyncStateError("Existing schedule ownership metadata has a different spec hash.")
        return SyncOwnershipMetadata(
            owner_id=existing.owner_id,
            action_provenance=existing.action_provenance,
            remote_fingerprint=schedule_record_fingerprint(remote),
            spec_hash=desired.schedule_spec_sha256,
            physical_target_id=existing.physical_target_id,
            creation_receipt_sha256=existing.creation_receipt_sha256,
            preimage_receipt_sha256=existing.preimage_receipt_sha256,
            deletion_policy=existing.deletion_policy,
        )
    provenance = receipt.action if receipt.action != "reused" else "reused"
    return SyncOwnershipMetadata(
        owner_id=desired.owner_id,
        action_provenance=provenance,
        remote_fingerprint=schedule_record_fingerprint(remote),
        spec_hash=desired.schedule_spec_sha256,
        physical_target_id=remote.schedule_id,
        creation_receipt_sha256=(receipt.sha256() if provenance == "created" else None),
        preimage_receipt_sha256=(receipt.sha256() if provenance == "updated" else None),
        deletion_policy={"created": "delete", "updated": "restore", "reused": "preserve"}[
            provenance
        ],
    )
