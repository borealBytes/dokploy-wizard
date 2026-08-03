"""Receipt-authorized crash-resumable sync schedule teardown."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from dokploy_wizard.dokploy.client import DokployApiClient, DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_ownership import require_owner
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.dokploy.shared_core_schedule_teardown import ScheduleTeardownReceipt
from dokploy_wizard.state import OwnedResource, load_state_dir
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.uninstall.errors import UninstallExecutionError


@dataclass(frozen=True, slots=True)
class SyncScheduleTeardown:
    values: Mapping[str, str]
    state_dir: Path
    api_url: str | None

    def delete(self, resource: OwnedResource) -> None:
        metadata = resource.metadata
        if metadata is None:
            raise UninstallExecutionError("Sync schedule deletion lacks ownership evidence.")
        if metadata.deletion_policy == "preserve":
            raise UninstallExecutionError("Preserve-only sync schedule cannot be deleted.")
        client = self.client()
        artifacts = SyncArtifactStore(self.state_dir)
        receipt_sha256 = (
            metadata.creation_receipt_sha256
            if metadata.deletion_policy == "delete"
            else metadata.preimage_receipt_sha256
        )
        if receipt_sha256 is None:
            raise UninstallExecutionError("Sync schedule lacks its teardown receipt.")
        receipt = artifacts.load_schedule_receipt(receipt_sha256)
        _require_receipt_bound_resource(resource, receipt)
        compose_id = receipt.remote.compose_id
        if compose_id is None:
            raise UninstallExecutionError("Sync schedule receipt has no compose id.")
        prior = artifacts.load_schedule_teardown(resource.resource_id)
        current = tuple(
            item
            for item in client.list_compose_schedules(compose_id=compose_id)
            if item.schedule_id == resource.resource_id
        )
        if prior is not None:
            _verify_completed(prior, current, receipt_sha256, resource)
            return
        if len(current) != 1:
            raise UninstallExecutionError("Owned sync schedule is missing or ambiguous.")
        require_owner(current[0], metadata.owner_id)
        current_fingerprint = schedule_record_fingerprint(current[0])
        if current_fingerprint != schedule_record_fingerprint(receipt.remote):
            loaded = load_state_dir(self.state_dir)
            desired = (
                None if loaded.desired_state is None else loaded.desired_state.opencode_go_sync
            )
            applied = (
                None if loaded.applied_state is None else loaded.applied_state.opencode_go_sync
            )
            tombstone_sha256 = None if applied is None else applied.disable_tombstone_sha256
            if desired is None or applied is None or tombstone_sha256 is None:
                raise UninstallExecutionError(
                    "Sync schedule fingerprint changed since its ownership receipt."
                )
            tombstone = artifacts.load_disable_tombstone(tombstone_sha256)
            if (
                tombstone.owner_id != metadata.owner_id
                or tombstone.schedule_id != resource.resource_id
                or tombstone.desired_fingerprint != desired.fingerprint()
                or applied.desired_fingerprint != desired.fingerprint()
                or tombstone.remote_fingerprint != current_fingerprint
            ):
                raise UninstallExecutionError(
                    "Sync schedule fingerprint changed since its ownership receipt."
                )
        if metadata.deletion_policy == "delete":
            if receipt.action != "created" or receipt.remote.schedule_id != resource.resource_id:
                raise UninstallExecutionError("Creation receipt does not authorize deletion.")
            client.delete_schedule(schedule_id=resource.resource_id)
            remaining = tuple(
                item
                for item in client.list_compose_schedules(compose_id=compose_id)
                if item.schedule_id == resource.resource_id
            )
            if remaining:
                raise UninstallExecutionError("Deleted sync schedule remains present.")
            artifacts.persist_schedule_teardown(
                _teardown_receipt(
                    resource,
                    receipt_sha256,
                    compose_id,
                    current[0],
                    None,
                )
            )
            return
        preimage = receipt.preimage
        if receipt.action != "updated" or preimage is None:
            raise UninstallExecutionError("Preimage receipt does not authorize restoration.")
        if preimage.service_name is None or preimage.timezone is None:
            raise UninstallExecutionError("Preimage receipt lacks required schedule fields.")
        client.update_schedule(
            schedule_id=preimage.schedule_id,
            name=preimage.name,
            compose_id=preimage.compose_id or "",
            service_name=preimage.service_name,
            cron_expression=preimage.cron_expression,
            timezone=preimage.timezone,
            shell_type=preimage.shell_type,
            command=preimage.command,
            enabled=preimage.enabled,
        )
        restored = tuple(
            item
            for item in client.list_compose_schedules(compose_id=compose_id)
            if item.schedule_id == resource.resource_id
        )
        if len(restored) != 1 or restored[0] != preimage:
            raise UninstallExecutionError("Restored sync schedule re-read does not match preimage.")
        artifacts.persist_schedule_teardown(
            _teardown_receipt(
                resource,
                receipt_sha256,
                compose_id,
                current[0],
                restored[0],
            )
        )

    def client(self) -> DokployApiClient:
        api_url = self.api_url or self.values.get("DOKPLOY_API_URL", "").strip()
        api_key = self.values.get("DOKPLOY_API_KEY", "").strip()
        if api_url == "" or api_key == "":
            raise UninstallExecutionError("Dokploy API credentials are required for sync teardown.")
        return DokployApiClient(api_url=api_url, api_key=api_key)


def _teardown_receipt(
    resource: OwnedResource,
    authorization_sha256: str,
    compose_id: str,
    before: DokployScheduleRecord,
    after: DokployScheduleRecord | None,
) -> ScheduleTeardownReceipt:
    metadata = resource.metadata
    if metadata is None:
        raise UninstallExecutionError("Sync teardown ownership metadata is missing.")
    return ScheduleTeardownReceipt(
        owner_id=metadata.owner_id,
        action="deleted" if after is None else "restored",
        schedule_id=resource.resource_id,
        compose_id=compose_id,
        authorization_receipt_sha256=authorization_sha256,
        before_sha256=schedule_record_fingerprint(before),
        after_sha256=None if after is None else schedule_record_fingerprint(after),
        completed_at=datetime.now(tz=UTC).isoformat(),
    )


def _verify_completed(
    teardown: ScheduleTeardownReceipt,
    current: tuple[DokployScheduleRecord, ...],
    authorization_sha256: str,
    resource: OwnedResource,
) -> None:
    metadata = resource.metadata
    if metadata is None:
        raise UninstallExecutionError("Sync teardown ownership metadata is missing.")
    expected_action = "deleted" if metadata.deletion_policy == "delete" else "restored"
    if (
        teardown.owner_id != metadata.owner_id
        or teardown.schedule_id != resource.resource_id
        or teardown.authorization_receipt_sha256 != authorization_sha256
        or teardown.action != expected_action
    ):
        raise UninstallExecutionError("Persisted sync teardown receipt does not authorize resume.")
    if teardown.action == "deleted":
        if current:
            raise UninstallExecutionError("Deleted sync schedule reappeared after teardown.")
        return
    if len(current) != 1 or schedule_record_fingerprint(current[0]) != teardown.after_sha256:
        raise UninstallExecutionError("Restored sync schedule drifted after teardown.")


def _require_receipt_bound_resource(
    resource: OwnedResource,
    receipt: ScheduleMutationReceipt,
) -> None:
    metadata = resource.metadata
    if metadata is None:
        raise UninstallExecutionError("Sync teardown ownership metadata is missing.")
    if (
        metadata.owner_id != receipt.owner_id
        or metadata.physical_target_id != resource.resource_id
        or receipt.remote.schedule_id != resource.resource_id
        or metadata.remote_fingerprint != schedule_record_fingerprint(receipt.remote)
    ):
        raise UninstallExecutionError("Sync schedule receipt does not bind its owned resource.")
