from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    DisableTombstone,
    ScheduleMutationReceipt,
)
from dokploy_wizard.dokploy.shared_core_sync_runtime import (
    SyncScheduleContext,
    reconcile_sync_schedule,
)
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.state.sync_schema import ScheduleSpec, SyncStateError

_OWNER_ID = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


def _schedule() -> DokployScheduleRecord:
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=_OWNER_ID,
    )
    return DokployScheduleRecord(
        schedule_id="schedule-1",
        name=spec.name,
        service_name=spec.service_name,
        cron_expression=spec.cron_expression,
        timezone=spec.timezone,
        shell_type=spec.shell_type,
        command=spec.command,
        enabled=spec.enabled,
        compose_id=spec.compose_id,
        schedule_type=spec.schedule_type,
    )


def test_schedule_spec_parser_rejects_noncanonical_owner_marked_command() -> None:
    spec = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=_OWNER_ID,
    )
    payload = spec.to_dict()
    payload["command"] = f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER_ID} true"

    with pytest.raises(SyncStateError, match="fixed contract"):
        ScheduleSpec.from_dict(payload)


def test_schedule_receipt_and_disable_tombstone_persist_as_canonical_bytes(
    tmp_path: Path,
) -> None:
    schedule = _schedule()
    receipt = ScheduleMutationReceipt(
        owner_id=_OWNER_ID,
        action="created",
        desired_fingerprint="a" * 64,
        before_spec_sha256=None,
        remote=schedule,
    )
    tombstone = DisableTombstone(
        owner_id=_OWNER_ID,
        schedule_id=schedule.schedule_id,
        desired_fingerprint="a" * 64,
        remote_fingerprint="b" * 64,
    )
    store = SyncArtifactStore(tmp_path)

    receipt_digest = store.persist_schedule_receipt(receipt)
    tombstone_digest = store.persist_disable_tombstone(tombstone)

    assert store.load_schedule_receipt(receipt_digest) == receipt
    assert store.load_disable_tombstone(tombstone_digest) == tombstone
    for path in (tmp_path / "shared-core-sync").glob("*/*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_bytes().endswith(b"\n")


def test_schedule_artifact_store_rejects_pathname_replacement(tmp_path: Path) -> None:
    receipt = ScheduleMutationReceipt(
        owner_id=_OWNER_ID,
        action="created",
        desired_fingerprint="a" * 64,
        before_spec_sha256=None,
        remote=_schedule(),
    )
    store = SyncArtifactStore(tmp_path)
    digest = store.persist_schedule_receipt(receipt)
    path = tmp_path / "shared-core-sync" / "schedule-receipts" / f"{digest}.json"
    path.write_bytes(replace(receipt, desired_fingerprint="c" * 64).sha256().encode())
    path.chmod(0o600)

    with pytest.raises(SyncStateError):
        store.load_schedule_receipt(digest)


@dataclass
class FakeScheduleClient:
    schedules: tuple[DokployScheduleRecord, ...] = ()

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        return tuple(item for item in self.schedules if item.compose_id == compose_id)

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        record = DokployScheduleRecord(
            schedule_id="schedule-1",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
            compose_id=compose_id,
            schedule_type="compose",
        )
        self.schedules = (*self.schedules, record)
        return record

    def update_schedule(
        self,
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        current = next(item for item in self.schedules if item.schedule_id == schedule_id)
        updated = replace(
            current,
            name=name,
            compose_id=compose_id,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = tuple(
            updated if item.schedule_id == schedule_id else item for item in self.schedules
        )
        return updated

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.schedules = tuple(
            item for item in self.schedules if item.schedule_id != schedule_id
        )


def _context(tmp_path: Path, *, enabled: bool = True) -> SyncScheduleContext:
    return SyncScheduleContext(
        stack_name="wizard",
        compose_id="compose-1",
        state_dir=tmp_path,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        enabled=enabled,
    )


def test_runtime_persists_create_update_reread_and_disable_documents(tmp_path: Path) -> None:
    client = FakeScheduleClient()

    created = reconcile_sync_schedule(
        client,
        _context(tmp_path),
        existing_applied=None,
        existing_metadata=None,
    )
    assert created.metadata is not None
    assert created.metadata.action_provenance == "created"
    receipt_store = SyncArtifactStore(tmp_path)
    created_receipt = receipt_store.load_schedule_receipt(
        created.applied.last_sync_receipt_sha256 or ""
    )
    assert created_receipt.action == "created"

    client.schedules = (replace(client.schedules[0], timezone="Etc/UTC"),)
    updated = reconcile_sync_schedule(
        client,
        _context(tmp_path),
        existing_applied=created.applied,
        existing_metadata=created.metadata,
    )
    assert updated.metadata is not None
    assert updated.metadata.action_provenance == "created"
    updated_receipt = receipt_store.load_schedule_receipt(
        updated.applied.last_sync_receipt_sha256 or ""
    )
    assert updated_receipt.action == "updated"
    assert updated_receipt.before_spec_sha256 is not None
    assert updated_receipt.remote.timezone == "UTC"

    disabled = reconcile_sync_schedule(
        client,
        _context(tmp_path, enabled=False),
        existing_applied=updated.applied,
        existing_metadata=updated.metadata,
    )
    tombstone_sha256 = disabled.applied.disable_tombstone_sha256
    assert tombstone_sha256 is not None
    assert receipt_store.load_disable_tombstone(tombstone_sha256).schedule_id == "schedule-1"

    reenabled = reconcile_sync_schedule(
        client,
        _context(tmp_path),
        existing_applied=disabled.applied,
        existing_metadata=disabled.metadata,
    )
    assert reenabled.applied.disable_tombstone_sha256 is None
    assert client.schedules[0].enabled is True
