# pyright: reportMissingImports=false

from __future__ import annotations

import fcntl
import multiprocessing
import os
from dataclasses import replace
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import pytest

from dokploy_wizard.cli import run_uninstall_flow
from dokploy_wizard.dokploy.sync_helper import CreateIntent, LeaseRequest
from dokploy_wizard.lifecycle.lock import ensure_lifecycle_stack_binding
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.sync_quiescence import (
    SyncScheduleQuiescence,
    SyncScheduleQuiescenceTimeout,
    quiesce_sync_schedule,
)

from .task11_authority_fakes import seed_creation_authority


def test_volume_owner_mismatch() -> None:
    owner = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    request = LeaseRequest(
        lease="lease-12345678",
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=12,
        parent_start_time_ticks=34,
        parent_argv_sha256="a" * 64,
        env=(("TZ", "b" * 64),),
        input_sha256="c" * 64,
        config_sha256="d" * 64,
        expected_state_sha256="e" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00+00:00",
    )

    with pytest.raises(SyncStateError, match="labels"):
        CreateIntent(
            lease=request.lease,
            name="wizard-opencode-go-lock-lease-12",
            stack="wizard",
            owner=owner,
            labels=(
                ("dokploy-wizard.stack", "wizard"),
                ("dokploy-wizard.owner", "99f90f71-8765-4aca-b83c-681f5ad74e80"),
                ("dokploy-wizard.lease", request.lease),
            ),
            image_digest="ghcr.io/berriai/litellm@sha256:" + "f" * 64,
            network="wizard-shared",
            volume_fingerprint="0" * 64,
            command_sha256="1" * 64,
            request_sha256=request.sha256(),
            mode=request.mode,
            container_id=None,
        )


FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _hold_sync_lock(
    lock_path: str,
    acquired: EventType,
    release: EventType,
) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired.set()
        release.wait(timeout=5)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sync_quiescence(metadata_root: Path) -> SyncScheduleQuiescence:
    owner_id = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
    desired = SyncDesiredState.from_schedule(
        owner_id=owner_id,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=ScheduleSpec.for_shared_core(
            stack_name="wizard",
            compose_id="compose-1",
            owner_id=owner_id,
        ),
    )
    resource = OwnedResource(
        resource_type="shared_core_sync_schedule",
        resource_id="schedule-1",
        scope="shared-core-sync:wizard",
        metadata=SyncOwnershipMetadata(
            owner_id=owner_id,
            action_provenance="created",
            remote_fingerprint="c" * 64,
            spec_hash=desired.schedule_spec_sha256,
            physical_target_id="schedule-1",
            creation_receipt_sha256="d" * 64,
            preimage_receipt_sha256=None,
            deletion_policy="delete",
        ),
    )
    return SyncScheduleQuiescence(
        metadata_root=metadata_root,
        resource=resource,
        desired=desired,
        applied=replace(
            AppliedSyncState.initial(desired),
            dokploy_schedule_id="schedule-1",
            compose_id="compose-1",
        ),
    )


def _attempt_catalog_recreate(lock_path: str, marker_path: str) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            Path(marker_path).write_text("blocked", encoding="utf-8")
            return
        Path(marker_path).write_text("recreated", encoding="utf-8")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_inflight_sync_teardown_race_and_delete_timeout_blocks_until_sync_lock_is_released(
    tmp_path: Path,
) -> None:
    quiescence = _sync_quiescence(tmp_path)
    process_context = multiprocessing.get_context("fork")
    acquired = process_context.Event()
    release = process_context.Event()
    worker = process_context.Process(
        target=_hold_sync_lock,
        args=(str(tmp_path / "sync.lock"), acquired, release),
    )
    worker.start()
    assert acquired.wait(timeout=5)

    try:
        with pytest.raises(SyncScheduleQuiescenceTimeout):
            with quiesce_sync_schedule(quiescence, timeout_seconds=0.05):
                pytest.fail("teardown acquired an in-flight sync lock")
    finally:
        release.set()
        worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
            pytest.fail("sync lock holder did not terminate")

    assert worker.exitcode == 0


def test_row_recreated_after_schedule_delete_is_blocked_by_quiescence(tmp_path: Path) -> None:
    quiescence = _sync_quiescence(tmp_path)
    marker = tmp_path / "catalog-result"
    deleted = tmp_path / "schedule-deleted"
    process_context = multiprocessing.get_context("fork")

    with quiesce_sync_schedule(quiescence):
        deleted.write_text("deleted", encoding="utf-8")
        worker = process_context.Process(
            target=_attempt_catalog_recreate,
            args=(str(tmp_path / "sync.lock"), str(marker)),
        )
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
            pytest.fail("catalog writer did not terminate")

        assert worker.exitcode == 0
        assert marker.read_text(encoding="utf-8") == "blocked"

    assert deleted.read_text(encoding="utf-8") == "deleted"


def _write_confirm_file(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _seed_state_dir(state_dir: Path) -> None:
    raw_input = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    desired_state = resolve_desired_state(raw_input)
    ensure_lifecycle_stack_binding(state_dir, desired_state.stack_name)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "headscale",
                "nextcloud",
            ),
        ),
    )


    ledger = OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource("cloudflare_tunnel", "nextcloud-stack-tunnel", "account:account-123"),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-dokploy.example.com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-headscale.example.com",
                    "zone:zone-123:headscale.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-nextcloud.example.com",
                    "zone:zone-123:nextcloud.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dns-onlyoffice.example.com",
                    "zone:zone-123:onlyoffice.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "nextcloud-stack-core",
                    "stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "nextcloud-stack-postgres",
                    "stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_redis",
                    "nextcloud-stack-redis",
                    "stack:nextcloud-stack:shared-redis",
                ),
                OwnedResource(
                    "shared_core_litellm",
                    "nextcloud-stack-litellm",
                    "stack:nextcloud-stack:shared-litellm",
                ),
                OwnedResource(
                    "headscale_service",
                    "nextcloud-stack-headscale",
                    "stack:nextcloud-stack:headscale",
                ),
                OwnedResource(
                    "nextcloud_service",
                    "nextcloud-stack-nextcloud",
                    "stack:nextcloud-stack:nextcloud-service",
                ),
                OwnedResource(
                    "onlyoffice_service",
                    "nextcloud-stack-onlyoffice",
                    "stack:nextcloud-stack:onlyoffice-service",
                ),
                OwnedResource(
                    "nextcloud_volume",
                    "nextcloud-stack-nextcloud-data",
                    "stack:nextcloud-stack:nextcloud-volume",
                ),
                OwnedResource(
                    "onlyoffice_volume",
                    "nextcloud-stack-onlyoffice-data",
                    "stack:nextcloud-stack:onlyoffice-volume",
                ),
            ),
    )
    write_ownership_ledger(state_dir, ledger)
    seed_creation_authority(state_dir, ledger.resources)


class RecordingUninstallBackend:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.deleted_resources: list[PlannedDeletion] = []
        self.receipted_resources: list[OwnedResource] = []

    def delete(self, deletion: PlannedDeletion) -> None:
        self.deleted_resources.append(deletion)
        UninstallAuthorityStore(self.state_dir).record_deletion(deletion.resource)
        self.receipted_resources.append(deletion.resource)


def test_retain_uninstall_preserves_data_resources_and_shrinks_checkpoint(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_state_dir(state_dir)
    confirm_file = _write_confirm_file(
        tmp_path / "retain.confirm",
        "# Retain-mode confirmation for nextcloud-stack",
        "Uninstall nextcloud-stack and retain data",
    )

    backend = RecordingUninstallBackend(state_dir)
    summary = run_uninstall_flow(
        state_dir=state_dir,
        destroy_data=False,
        dry_run=False,
        non_interactive=True,
        confirm_file=confirm_file,
        uninstall_backend=backend,
        stack_name="nextcloud-stack",
    )

    loaded = load_state_dir(state_dir)
    assert summary["mode"] == "retain"
    assert summary["state_cleared"] is False
    assert summary["remaining_completed_steps"] == ["preflight", "dokploy_bootstrap"]
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == ("preflight", "dokploy_bootstrap")
    assert loaded.ownership_ledger is not None
    authority = UninstallAuthorityStore(state_dir)
    assert {resource.resource_type for resource in loaded.ownership_ledger.resources} == {
        "shared_core_postgres",
        "shared_core_redis",
        "nextcloud_volume",
        "onlyoffice_volume",
    }
    assert backend.deleted_resources
    assert backend.receipted_resources == [item.resource for item in backend.deleted_resources]
    assert all(
        authority.load_created(resource) is not None
        and authority.load_deletion(resource) is None
        for resource in loaded.ownership_ledger.resources
    )


def test_destroy_uninstall_clears_state_when_nothing_owned_remains(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_state_dir(state_dir)
    confirm_file = _write_confirm_file(
        tmp_path / "destroy.confirm",
        "# Destroy-mode confirmation for nextcloud-stack",
        "I understand this is destructive",
        "Destroy data for this environment",
        "Destroy all data for nextcloud-stack",
    )

    backend = RecordingUninstallBackend(state_dir)
    summary = run_uninstall_flow(
        state_dir=state_dir,
        destroy_data=True,
        dry_run=False,
        non_interactive=True,
        confirm_file=confirm_file,
        uninstall_backend=backend,
        stack_name="nextcloud-stack",
    )

    loaded = load_state_dir(state_dir)
    assert summary["mode"] == "destroy"
    assert summary["state_cleared"] is True
    assert loaded.raw_input is None
    assert loaded.desired_state is None
    assert loaded.applied_state is None
    assert loaded.ownership_ledger is None
    assert backend.deleted_resources
    assert backend.receipted_resources == [item.resource for item in backend.deleted_resources]
