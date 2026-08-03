from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dokploy_wizard.dokploy import sync_helper
from dokploy_wizard.state.shared_core_sync import SyncStateError

_DIGEST = "a" * 64


def _request() -> sync_helper.LeaseRequest:
    return sync_helper.LeaseRequest(
        lease="lease-12345678",
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=12,
        parent_start_time_ticks=34,
        parent_argv_sha256=_DIGEST,
        env=(("TZ", "b" * 64),),
        input_sha256="c" * 64,
        config_sha256="d" * 64,
        expected_state_sha256="e" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00+00:00",
    )


def _create_intent() -> sync_helper.CreateIntent:
    return sync_helper.CreateIntent.for_request(
        request=_request(),
        stack="wizard",
        owner="3b8e1e83-0e57-4d66-a65e-1edbf2aac838",
        image_digest="ghcr.io/berriai/litellm@sha256:" + "f" * 64,
        network="wizard-shared",
        volume_fingerprint="1" * 64,
        command_sha256="2" * 64,
    )


def _container(intent: sync_helper.CreateIntent) -> sync_helper.HelperContainerObservation:
    return sync_helper.HelperContainerObservation(
        container_id="3" * 64,
        name=intent.name,
        labels=intent.labels,
        image_digest=intent.image_digest,
        network=intent.network,
        volume_fingerprint=intent.volume_fingerprint,
        command_sha256=intent.command_sha256,
    )


def test_exact_lease_request_result_release_schemas_round_trip() -> None:
    request = _request()
    result_type = getattr(sync_helper, "LeaseResult")
    release_type = getattr(sync_helper, "LeaseRelease")
    result = result_type(
        lease=request.lease,
        generation=request.generation,
        request_sha256=request.sha256(),
        status="succeeded",
        parent_exit_code=0,
        before_snapshot_sha256="1" * 64,
        after_snapshot_sha256="2" * 64,
        parent_reconcile_sha256="3" * 64,
        durable_write_delta=("applied-state.json",),
        started_at="2026-07-28T00:00:00+00:00",
        ended_at="2026-07-28T00:01:00+00:00",
    )
    release = release_type(
        lease=request.lease,
        generation=request.generation,
        expected_receipt_version=7,
        expected_result_sha256=result.sha256(),
        requested_at="2026-07-28T00:01:01+00:00",
    )

    assert type(request).from_dict(request.to_dict()) == request
    assert result_type.from_dict(result.to_dict()) == result
    assert release_type.from_dict(release.to_dict()) == release
    with pytest.raises(SyncStateError, match="exact keys"):
        result_type.from_dict({**result.to_dict(), "unknown": True})


def test_create_intent_round_trips_and_persists_full_id_before_start() -> None:
    intent = _create_intent()
    persisted: list[sync_helper.CreateIntent] = []

    created = sync_helper.bind_created_container(
        intent,
        matches=(),
        create_container=lambda: _container(intent),
        persist_created=persisted.append,
    )

    assert sync_helper.CreateIntent.from_dict(intent.to_dict()) == intent
    assert persisted == [created]
    assert created.phase == "created"
    assert created.container_id == "3" * 64


def test_create_intent_recovery_rejects_ambiguous_or_mismatched_containers() -> None:
    intent = _create_intent()
    exact = _container(intent)
    with pytest.raises(SyncStateError, match="Multiple"):
        sync_helper.bind_created_container(
            intent,
            matches=(exact, replace(exact, container_id="4" * 64)),
            create_container=lambda: exact,
            persist_created=lambda _: None,
        )
    with pytest.raises(SyncStateError, match="exact create intent"):
        sync_helper.bind_created_container(
            intent,
            matches=(replace(exact, network="foreign"),),
            create_container=lambda: exact,
            persist_created=lambda _: None,
        )


def test_lease_phase_transition_requires_generation_version_cas() -> None:
    receipt = sync_helper.LeaseReceipt.created(request=_request(), container_id="f" * 64)
    advance = getattr(sync_helper, "advance_lease_receipt")

    assert sync_helper.LeaseReceipt.from_dict(receipt.to_dict()) == receipt

    starting = advance(
        receipt,
        next_phase="starting",
        expected_generation=1,
        expected_receipt_version=1,
        heartbeat_at="2026-07-28T00:00:01+00:00",
        heartbeat_deadline_at="2026-07-28T00:05:01+00:00",
    )

    assert starting.generation == 2
    assert starting.receipt_version == 2
    with pytest.raises(SyncStateError, match="CAS"):
        advance(
            starting,
            next_phase="acquired_held",
            expected_generation=1,
            expected_receipt_version=2,
        )
    with pytest.raises(SyncStateError, match="transition"):
        advance(
            starting,
            next_phase="released",
            expected_generation=2,
            expected_receipt_version=2,
        )


def test_lease_freshness_is_bounded_by_heartbeat_deadline() -> None:
    receipt = replace(
        sync_helper.LeaseReceipt.created(request=_request(), container_id="f" * 64),
        phase="parent_running",
        heartbeat_at="2026-07-28T00:00:00+00:00",
        heartbeat_deadline_at="2026-07-28T00:05:00+00:00",
        lock_inode=123,
    )
    is_fresh = getattr(sync_helper, "lease_is_fresh")

    assert is_fresh(receipt, datetime(2026, 7, 28, 0, 5, tzinfo=UTC))
    assert not is_fresh(
        receipt,
        datetime(2026, 7, 28, 0, 5, tzinfo=UTC) + timedelta(microseconds=1),
    )


def test_parent_identity_rejects_pid_reuse_start_time_and_argv_mismatch(tmp_path: Path) -> None:
    proc = tmp_path / "proc" / "12"
    proc.mkdir(parents=True)
    stat = "12 (parent name) S " + " ".join(["0"] * 18 + ["34"])
    (proc / "stat").write_text(stat, encoding="utf-8")
    (proc / "cmdline").write_bytes(b"python\x00parent.py\x00")
    identity_type = getattr(sync_helper, "ParentIdentity")
    read_identity = getattr(sync_helper, "read_parent_identity")
    expected = identity_type(
        pid=12,
        start_time_ticks=34,
        argv_sha256=read_identity(12, proc_root=tmp_path / "proc").argv_sha256,
    )

    assert read_identity(12, proc_root=tmp_path / "proc") == expected
    with pytest.raises(SyncStateError, match="identity"):
        getattr(sync_helper, "verify_parent_identity")(
            replace(expected, start_time_ticks=35),
            proc_root=tmp_path / "proc",
        )
    with pytest.raises(SyncStateError, match="identity"):
        getattr(sync_helper, "verify_parent_identity")(
            replace(expected, argv_sha256="0" * 64),
            proc_root=tmp_path / "proc",
        )
