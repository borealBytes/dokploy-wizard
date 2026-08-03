from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.sync_helper import (
    LeaseReceipt,
    LeaseRequest,
    ParentIdentity,
    advance_lease_receipt,
    verify_parent_identity,
)
from dokploy_wizard.state.sync_schema import SyncStateError


def _request() -> LeaseRequest:
    return LeaseRequest(
        lease="97099d6d-fd71-4ae0-8779-df6de483dfcc",
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=123,
        parent_start_time_ticks=456,
        parent_argv_sha256="1" * 64,
        env=(("TZ", "2" * 64),),
        input_sha256="3" * 64,
        config_sha256="4" * 64,
        expected_state_sha256="5" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00Z",
    )


def test_every_successful_lease_phase_requires_generation_and_version_cas() -> None:
    receipt = LeaseReceipt.created(request=_request(), container_id="f" * 64)
    phases = (
        "starting",
        "acquired_held",
        "parent_running",
        "after_snapshot_written",
        "release_requested",
        "released",
    )

    for phase in phases:
        previous = receipt
        receipt = advance_lease_receipt(
            previous,
            next_phase=phase,
            expected_generation=previous.generation,
            expected_receipt_version=previous.receipt_version,
            heartbeat_at="2026-07-28T00:00:01Z",
            heartbeat_deadline_at="2026-07-28T00:05:01Z",
            lock_inode=123,
            result_sha256="6" * 64 if phase == "after_snapshot_written" else None,
        )
        assert receipt.generation == previous.generation + 1
        assert receipt.receipt_version == previous.receipt_version + 1

    assert receipt.phase == "released"
    with pytest.raises(SyncStateError, match="transition"):
        advance_lease_receipt(
            receipt,
            next_phase="failed",
            expected_generation=receipt.generation,
            expected_receipt_version=receipt.receipt_version,
        )


def test_dead_parent_identity_fails_before_helper_lock_acquisition(tmp_path: Path) -> None:
    expected = ParentIdentity(
        pid=123,
        start_time_ticks=456,
        argv_sha256="1" * 64,
    )

    with pytest.raises(SyncStateError, match="[Pp]arent"):
        verify_parent_identity(expected, proc_root=tmp_path / "proc")
