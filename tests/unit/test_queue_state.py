# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from dokploy_wizard.state import DurableQueueStore, load_inbox_event_log, load_job_queue_state


def _ts(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 19, hour, minute, second, tzinfo=UTC)


def test_duplicate_ingress_event_and_job_collapse_by_idempotency(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)

    first_event = store.persist_incoming_event(
        source="nextcloud-talk",
        idempotency_key="talk:evt-1",
        raw_body=b'{"event":"message"}',
        parsed_payload={"event": "message"},
        received_at=_ts(12, 0),
    )
    second_event = store.persist_incoming_event(
        source="nextcloud-talk",
        idempotency_key="talk:evt-1",
        raw_body=b'{"event":"message"}',
        parsed_payload={"event": "message"},
        received_at=_ts(12, 1),
    )

    first_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-1"},
        idempotency_key="talk:evt-1",
        scope_key="talk-room:room-42",
        supersession_key="talk-thread:room-42:msg-840",
        now=_ts(12, 0),
    )
    second_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-1"},
        idempotency_key="talk:evt-1",
        scope_key="talk-room:room-42",
        supersession_key="talk-thread:room-42:msg-840",
        now=_ts(12, 1),
    )

    inbox = load_inbox_event_log(tmp_path)
    queue = load_job_queue_state(tmp_path)

    assert first_event == second_event
    assert first_job == second_job
    assert len(inbox.events) == 1
    assert len(queue.jobs) == 1


def test_retry_then_dead_letter_after_attempt_budget_is_exhausted(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    enqueued = store.enqueue_job(
        queue="background",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1"},
        idempotency_key="onlyoffice:doc-1:status:2",
        scope_key="document:doc-1",
        max_attempts=2,
        now=_ts(13, 0),
    )

    first_lease = store.lease_next_job(lease_owner="worker-a", now=_ts(13, 0, 5))
    assert first_lease is not None
    assert first_lease.job_id == enqueued.job_id
    assert first_lease.status == "leased"
    assert first_lease.attempt_count == 1

    retry_job = store.mark_job_failed(
        job_id=enqueued.job_id,
        error_message="temporary upstream failure",
        now=_ts(13, 1),
        retry_delay=timedelta(minutes=5),
    )
    assert retry_job.status == "retry"
    assert retry_job.attempt_count == 1
    assert retry_job.last_error == "temporary upstream failure"
    assert retry_job.run_after == _ts(13, 6).isoformat()

    assert store.lease_next_job(lease_owner="worker-a", now=_ts(13, 5, 59)) is None

    second_lease = store.lease_next_job(lease_owner="worker-b", now=_ts(13, 6))
    assert second_lease is not None
    assert second_lease.attempt_count == 2

    dead_letter_job = store.mark_job_failed(
        job_id=enqueued.job_id,
        error_message="permanent upstream failure",
        now=_ts(13, 7),
        retry_delay=timedelta(minutes=5),
    )
    assert dead_letter_job.status == "dead_letter"
    assert dead_letter_job.attempt_count == 2
    assert dead_letter_job.last_error == "permanent upstream failure"


def test_new_superseding_job_cancels_older_runnable_job(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)

    old_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1", "status": 2},
        idempotency_key="onlyoffice:doc-1:version:171",
        scope_key="document:doc-1",
        supersession_key="document:doc-1:latest-save",
        now=_ts(14, 0),
    )
    new_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1", "status": 2},
        idempotency_key="onlyoffice:doc-1:version:172",
        scope_key="document:doc-1",
        supersession_key="document:doc-1:latest-save",
        now=_ts(14, 1),
    )

    queue = load_job_queue_state(tmp_path)
    old_state = next(job for job in queue.jobs if job.job_id == old_job.job_id)
    new_state = next(job for job in queue.jobs if job.job_id == new_job.job_id)

    assert old_state.status == "superseded"
    assert old_state.last_error == f"Superseded by newer job {new_job.job_id}."
    assert new_state.status == "queued"


def test_new_superseding_job_does_not_cancel_older_leased_job(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)

    old_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1", "status": 2},
        idempotency_key="onlyoffice:doc-1:version:171",
        scope_key="document:doc-1",
        supersession_key="document:doc-1:latest-save",
        now=_ts(14, 30),
    )
    leased_job = store.lease_next_job(lease_owner="worker-a", now=_ts(14, 31))
    assert leased_job is not None
    assert leased_job.job_id == old_job.job_id
    assert leased_job.status == "leased"

    new_job = store.enqueue_job(
        queue="foreground",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1", "status": 2},
        idempotency_key="onlyoffice:doc-1:version:172",
        scope_key="document:doc-1",
        supersession_key="document:doc-1:latest-save",
        now=_ts(14, 32),
    )

    queue = load_job_queue_state(tmp_path)
    old_state = next(job for job in queue.jobs if job.job_id == old_job.job_id)
    new_state = next(job for job in queue.jobs if job.job_id == new_job.job_id)

    assert old_state.status == "leased"
    assert old_state.lease_owner == "worker-a"
    assert old_state.last_error is None
    assert new_state.status == "queued"


def test_scope_serialization_blocks_same_scope_until_current_lease_finishes(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    first = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-1"},
        idempotency_key="talk:evt-1",
        scope_key="talk-room:room-42",
        now=_ts(15, 0),
    )
    second = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-2"},
        idempotency_key="talk:evt-2",
        scope_key="talk-room:room-42",
        now=_ts(15, 1),
    )

    leased = store.lease_next_job(lease_owner="worker-a", now=_ts(15, 2))
    assert leased is not None
    assert leased.job_id == first.job_id
    assert store.lease_next_job(lease_owner="worker-b", now=_ts(15, 3)) is None

    completed = store.mark_job_completed(job_id=first.job_id, now=_ts(15, 4))
    assert completed.status == "completed"

    next_job = store.lease_next_job(lease_owner="worker-b", now=_ts(15, 5))
    assert next_job is not None
    assert next_job.job_id == second.job_id


def test_global_single_thread_default_blocks_cross_scope_lease_until_active_work_finishes(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    first = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-1"},
        idempotency_key="talk:evt-1",
        scope_key="talk-room:room-42",
        now=_ts(15, 30),
    )
    second = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-2"},
        idempotency_key="talk:evt-2",
        scope_key="talk-room:room-43",
        now=_ts(15, 31),
    )

    leased = store.lease_next_job(lease_owner="worker-a", now=_ts(15, 32))
    assert leased is not None
    assert leased.job_id == first.job_id
    assert store.lease_next_job(lease_owner="worker-b", now=_ts(15, 33)) is None

    completed = store.mark_job_completed(job_id=first.job_id, now=_ts(15, 34))
    assert completed.status == "completed"

    next_job = store.lease_next_job(lease_owner="worker-b", now=_ts(15, 35))
    assert next_job is not None
    assert next_job.job_id == second.job_id


def test_foreground_jobs_get_priority_without_starving_background_work(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    foreground_one = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-1"},
        idempotency_key="talk:evt-foreground-1",
        scope_key="talk-room:room-1",
        now=_ts(16, 0),
    )
    foreground_two = store.enqueue_job(
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={"message_id": "msg-2"},
        idempotency_key="talk:evt-foreground-2",
        scope_key="talk-room:room-2",
        now=_ts(16, 1),
    )
    background_one = store.enqueue_job(
        queue="background",
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload={"document_key": "doc-1"},
        idempotency_key="onlyoffice:doc-1:status:6",
        scope_key="document:doc-1",
        now=_ts(16, 2),
    )

    lease_one = store.lease_next_job(lease_owner="worker-a", now=_ts(16, 3))
    assert lease_one is not None
    assert lease_one.job_id == foreground_one.job_id
    store.mark_job_completed(job_id=foreground_one.job_id, now=_ts(16, 4))

    lease_two = store.lease_next_job(lease_owner="worker-a", now=_ts(16, 5))
    assert lease_two is not None
    assert lease_two.job_id == foreground_two.job_id
    store.mark_job_completed(job_id=foreground_two.job_id, now=_ts(16, 6))

    lease_three = store.lease_next_job(lease_owner="worker-a", now=_ts(16, 7))
    assert lease_three is not None
    assert lease_three.job_id == background_one.job_id

    queue = load_job_queue_state(tmp_path)
    assert queue.foreground_streak == 0
