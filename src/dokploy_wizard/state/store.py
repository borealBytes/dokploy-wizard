# mypy: ignore-errors
# ruff: noqa: E501
"""State-directory loading and persistence helpers."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, TypeVar

from dokploy_wizard.state.models import (
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    STATE_FORMAT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    LiteLLMGeneratedKeys,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
)
from dokploy_wizard.state.queue_models import (
    DurableJobRecord,
    InboxEventLog,
    InboxEventRecord,
    JobQueueState,
    OutboundDeliveryLog,
    OutboundDeliveryRecord,
)

RAW_INPUT_FILE = "raw-input.json"
DESIRED_STATE_FILE = "desired-state.json"
APPLIED_STATE_FILE = "applied-state.json"
OWNERSHIP_LEDGER_FILE = "ownership-ledger.json"
INBOX_EVENTS_FILE = "inbox-events.json"
JOB_QUEUE_FILE = "job-queue.json"
OUTBOUND_DELIVERIES_FILE = "outbound-deliveries.json"
LITELLM_GENERATED_KEYS_FILE = "litellm-generated-keys.json"
STATE_DOCUMENT_FILES = (
    RAW_INPUT_FILE,
    DESIRED_STATE_FILE,
    APPLIED_STATE_FILE,
    OWNERSHIP_LEDGER_FILE,
)

_DocumentT = TypeVar("_DocumentT")


@dataclass(frozen=True)
class LoadedState:
    raw_input: RawEnvInput | None
    desired_state: DesiredState | None
    applied_state: AppliedStateCheckpoint | None
    ownership_ledger: OwnershipLedger | None


@dataclass(frozen=True)
class DurableQueueStore:
    """Small JSON-backed store for durable inbox and queue runtime state."""

    state_dir: Path

    def persist_incoming_event(
        self,
        *,
        source: str,
        idempotency_key: str,
        raw_body: bytes,
        parsed_payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> InboxEventRecord:
        event_log = load_inbox_event_log(self.state_dir)
        existing = next(
            (
                event
                for event in event_log.events
                if event.source == source and event.idempotency_key == idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing

        timestamp = _timestamp(received_at)
        event = InboxEventRecord(
            format_version=STATE_FORMAT_VERSION,
            event_id=_stable_identity("event", f"{source}:{idempotency_key}"),
            source=source,
            idempotency_key=idempotency_key,
            received_at=timestamp,
            raw_body=raw_body,
            parsed_payload=parsed_payload,
        )
        write_inbox_event_log(
            self.state_dir,
            InboxEventLog(
                format_version=STATE_FORMAT_VERSION,
                events=event_log.events + (event,),
            ),
        )
        return event

    def enqueue_job(
        self,
        *,
        queue: str,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        scope_key: str,
        supersession_key: str | None = None,
        max_attempts: int = 3,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        queue_state = load_job_queue_state(self.state_dir)
        existing = next(
            (job for job in queue_state.jobs if job.idempotency_key == idempotency_key),
            None,
        )
        if existing is not None:
            return existing

        created_at = _timestamp(now)
        new_job = DurableJobRecord(
            format_version=STATE_FORMAT_VERSION,
            job_id=_stable_identity("job", idempotency_key),
            kind=job_type,
            scope_key=scope_key,
            supersession_key=supersession_key,
            lane=_normalize_queue_name(queue),
            status="queued",
            attempt_count=0,
            max_attempts=max_attempts,
            run_after=_timestamp(run_after or now),
            lease_owner=None,
            leased_until=None,
            created_at=created_at,
            updated_at=created_at,
            last_error=None,
            last_error_at=None,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        policy = _queue_policy_module()
        updated_jobs = policy.apply_supersession(
            queue_state.jobs,
            scope_key=scope_key,
            supersession_key=supersession_key,
            now=created_at,
            replacement_job_id=new_job.job_id,
        )
        write_job_queue_state(
            self.state_dir,
            JobQueueState(
                format_version=STATE_FORMAT_VERSION,
                jobs=updated_jobs + (new_job,),
                foreground_streak=queue_state.foreground_streak,
            ),
        )
        return new_job

    def get_incoming_event(
        self,
        *,
        source: str,
        idempotency_key: str,
    ) -> InboxEventRecord | None:
        event_log = load_inbox_event_log(self.state_dir)
        return next(
            (
                event
                for event in event_log.events
                if event.source == source and event.idempotency_key == idempotency_key
            ),
            None,
        )

    def get_outbound_delivery(
        self,
        *,
        channel: str,
        delivery_key: str,
    ) -> OutboundDeliveryRecord | None:
        delivery_log = load_outbound_delivery_log(self.state_dir)
        return next(
            (
                record
                for record in delivery_log.records
                if record.channel == channel and record.delivery_key == delivery_key
            ),
            None,
        )

    def record_outbound_delivery(
        self,
        *,
        channel: str,
        delivery_key: str,
        transport: str,
        scope_key: str,
        conversation_id: str,
        reply_to_message_id: str,
        thread_mode: str,
        thread_id: str | None,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> OutboundDeliveryRecord:
        delivery_log = load_outbound_delivery_log(self.state_dir)
        existing = next(
            (
                record
                for record in delivery_log.records
                if record.channel == channel and record.delivery_key == delivery_key
            ),
            None,
        )
        if existing is not None:
            return existing

        timestamp = _timestamp(now)
        record = OutboundDeliveryRecord(
            format_version=STATE_FORMAT_VERSION,
            delivery_id=_stable_identity("delivery", f"{channel}:{delivery_key}"),
            channel=channel,
            delivery_key=delivery_key,
            transport=transport,
            scope_key=scope_key,
            conversation_id=conversation_id,
            reply_to_message_id=reply_to_message_id,
            thread_mode=thread_mode,
            thread_id=thread_id,
            status="pending",
            created_at=timestamp,
            updated_at=timestamp,
            remote_message_id=None,
            remote_request_id=None,
            payload=payload,
        )
        write_outbound_delivery_log(
            self.state_dir,
            OutboundDeliveryLog(
                format_version=STATE_FORMAT_VERSION,
                records=delivery_log.records + (record,),
            ),
        )
        return record

    def mark_outbound_delivery_sent(
        self,
        *,
        channel: str,
        delivery_key: str,
        remote_message_id: str,
        remote_request_id: str | None = None,
        now: datetime | None = None,
    ) -> OutboundDeliveryRecord:
        delivery_log = load_outbound_delivery_log(self.state_dir)
        target = next(
            (
                record
                for record in delivery_log.records
                if record.channel == channel and record.delivery_key == delivery_key
            ),
            None,
        )
        if target is None:
            msg = f"Unknown outbound delivery '{channel}:{delivery_key}'."
            raise StateValidationError(msg)

        timestamp = _timestamp(now)
        updated = replace(
            target,
            status="sent",
            updated_at=timestamp,
            remote_message_id=remote_message_id,
            remote_request_id=remote_request_id,
        )
        write_outbound_delivery_log(
            self.state_dir,
            OutboundDeliveryLog(
                format_version=STATE_FORMAT_VERSION,
                records=tuple(
                    updated if record.delivery_id == target.delivery_id else record
                    for record in delivery_log.records
                ),
            ),
        )
        return updated

    def lease_next_job(
        self,
        *,
        lease_owner: str,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> DurableJobRecord | None:
        timestamp = _timestamp(now)
        policy = _queue_policy_module()
        queue_state = policy.sweep_expired_leases(load_job_queue_state(self.state_dir), now=timestamp)
        selected = policy.choose_next_job(queue_state, now=timestamp)
        if selected is None:
            write_job_queue_state(self.state_dir, queue_state)
            return None

        leased_until = _timestamp(_as_datetime(now) + lease_duration)
        leased_job = replace(
            selected,
            status="leased",
            attempt_count=selected.attempt_count + 1,
            lease_owner=lease_owner,
            leased_until=leased_until,
            updated_at=timestamp,
        )
        updated_jobs = tuple(
            leased_job if job.job_id == selected.job_id else job for job in queue_state.jobs
        )
        write_job_queue_state(
            self.state_dir,
            JobQueueState(
                format_version=STATE_FORMAT_VERSION,
                jobs=updated_jobs,
                foreground_streak=policy.next_foreground_streak(
                    queue_state.foreground_streak,
                    leased_lane=leased_job.lane,
                ),
            ),
        )
        return leased_job

    def mark_job_completed(
        self,
        *,
        job_id: str,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        return self._update_job_terminal_state(
            job_id=job_id,
            status="completed",
            error_message=None,
            retry_delay=None,
            now=now,
        )

    def mark_job_failed(
        self,
        *,
        job_id: str,
        error_message: str,
        now: datetime | None = None,
        retry_delay: timedelta = timedelta(minutes=1),
    ) -> DurableJobRecord:
        return self._update_job_terminal_state(
            job_id=job_id,
            status="dead_letter",
            error_message=error_message,
            retry_delay=retry_delay,
            now=now,
        )

    def _update_job_terminal_state(
        self,
        *,
        job_id: str,
        status: str,
        error_message: str | None,
        retry_delay: timedelta | None,
        now: datetime | None,
    ) -> DurableJobRecord:
        queue_state = load_job_queue_state(self.state_dir)
        target = next((job for job in queue_state.jobs if job.job_id == job_id), None)
        if target is None:
            msg = f"Unknown job id '{job_id}'."
            raise StateValidationError(msg)
        if target.status != "leased":
            msg = f"Job '{job_id}' must be leased before it can transition."
            raise StateValidationError(msg)

        timestamp = _timestamp(now)
        if error_message is None:
            updated_target = replace(
                target,
                status=status,
                lease_owner=None,
                leased_until=None,
                updated_at=timestamp,
            )
        elif target.attempt_count >= target.max_attempts:
            updated_target = replace(
                target,
                status="dead_letter",
                lease_owner=None,
                leased_until=None,
                updated_at=timestamp,
                last_error=error_message,
                last_error_at=timestamp,
            )
        else:
            updated_target = replace(
                target,
                status="retry",
                lease_owner=None,
                leased_until=None,
                run_after=_timestamp(_as_datetime(now) + (retry_delay or timedelta(minutes=1))),
                updated_at=timestamp,
                last_error=error_message,
                last_error_at=timestamp,
            )
        updated_jobs = tuple(
            updated_target if job.job_id == target.job_id else job for job in queue_state.jobs
        )
        write_job_queue_state(
            self.state_dir,
            JobQueueState(
                format_version=STATE_FORMAT_VERSION,
                jobs=updated_jobs,
                foreground_streak=queue_state.foreground_streak,
            ),
        )
        return updated_target


def load_state_dir(state_dir: Path) -> LoadedState:
    return LoadedState(
        raw_input=_load_optional_document(state_dir / RAW_INPUT_FILE, RawEnvInput.from_dict),
        desired_state=_load_optional_document(
            state_dir / DESIRED_STATE_FILE, DesiredState.from_dict
        ),
        applied_state=_load_optional_document(
            state_dir / APPLIED_STATE_FILE,
            AppliedStateCheckpoint.from_dict,
        ),
        ownership_ledger=_load_optional_document(
            state_dir / OWNERSHIP_LEDGER_FILE,
            OwnershipLedger.from_dict,
        ),
    )


def write_inspection_snapshot(
    state_dir: Path, raw_input: RawEnvInput, desired_state_snapshot: dict[str, Any]
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state_snapshot)


def load_litellm_generated_keys(state_dir: Path) -> LiteLLMGeneratedKeys | None:
    return _load_optional_document(
        state_dir / LITELLM_GENERATED_KEYS_FILE,
        LiteLLMGeneratedKeys.from_dict,
    )


def write_litellm_generated_keys(state_dir: Path, generated_keys: LiteLLMGeneratedKeys) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / LITELLM_GENERATED_KEYS_FILE
    _write_document(path, generated_keys.to_dict())
    path.chmod(0o600)


def ensure_litellm_generated_keys(state_dir: Path) -> LiteLLMGeneratedKeys:
    existing = load_litellm_generated_keys(state_dir)
    if existing is not None:
        return existing

    generated_keys = LiteLLMGeneratedKeys(
        format_version=STATE_FORMAT_VERSION,
        master_key=_generate_secret(prefix="litellm-master"),
        salt_key=_generate_secret(prefix="litellm-salt"),
        virtual_keys={
            "coder-hermes": _generate_secret(prefix="litellm-coder-hermes"),
            "coder-kdense": _generate_secret(prefix="litellm-coder-kdense"),
            "my-farm-advisor": _generate_secret(prefix="litellm-my-farm-advisor"),
            "openclaw": _generate_secret(prefix="litellm-openclaw"),
        },
    )
    write_litellm_generated_keys(state_dir, generated_keys)
    return generated_keys


def validate_install_state(loaded_state: LoadedState, desired_state: DesiredState) -> bool:
    """Validate existing install state and report whether it already exists."""

    _validate_state_document_set(loaded_state)

    if loaded_state.desired_state is None:
        return False
    if loaded_state.applied_state is None:
        return False

    if loaded_state.desired_state.to_dict() != desired_state.to_dict():
        msg = "Existing desired state does not match this install request."
        raise StateValidationError(msg)

    if loaded_state.applied_state.desired_state_fingerprint != desired_state.fingerprint():
        msg = "Existing applied state fingerprint does not match the desired state."
        raise StateValidationError(msg)

    return True


def validate_existing_state(loaded_state: LoadedState) -> bool:
    """Validate the current state-dir document set without requiring a matching target."""

    _validate_state_document_set(loaded_state)
    return loaded_state.desired_state is not None


def write_target_state(
    state_dir: Path, raw_input: RawEnvInput, desired_state: DesiredState
) -> None:
    """Persist the requested raw input and desired state before mutating phases."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state.to_dict())


def _validate_state_document_set(loaded_state: LoadedState) -> None:
    """Validate all-or-none state documents plus supported checkpoint step names."""

    documents_present = {
        "raw input": loaded_state.raw_input is not None,
        "desired state": loaded_state.desired_state is not None,
        "applied state": loaded_state.applied_state is not None,
        "ownership ledger": loaded_state.ownership_ledger is not None,
    }
    present_count = sum(documents_present.values())
    if present_count == 0:
        return
    if present_count != len(documents_present):
        missing = sorted(name for name, present in documents_present.items() if not present)
        msg = (
            "Invalid existing state: expected raw input, desired state, applied state, "
            f"and ownership ledger together; missing {', '.join(missing)}."
        )
        raise StateValidationError(msg)

    assert loaded_state.applied_state is not None

    allowed_steps = {
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "seaweedfs",
        "headscale",
        "tailscale",
        "matrix",
        "nextcloud",
        "moodle",
        "docuseal",
        "coder",
        "openclaw",
        "my-farm-advisor",
        "cloudflare_access",
    }
    unexpected_steps = sorted(
        step for step in loaded_state.applied_state.completed_steps if step not in allowed_steps
    )
    if unexpected_steps:
        msg = f"Existing applied state contains unsupported completed steps: {unexpected_steps}."
        raise StateValidationError(msg)


def persist_install_scaffold(
    state_dir: Path, raw_input: RawEnvInput, desired_state: DesiredState
) -> None:
    """Persist the initial Task 3 state scaffold before bootstrap mutation."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state.to_dict())
    _write_document(
        state_dir / APPLIED_STATE_FILE,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(),
            lifecycle_checkpoint_contract_version=LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
        ).to_dict(),
    )
    _write_document(
        state_dir / OWNERSHIP_LEDGER_FILE,
        OwnershipLedger(format_version=desired_state.format_version, resources=()).to_dict(),
    )


def write_applied_checkpoint(state_dir: Path, applied_state: AppliedStateCheckpoint) -> None:
    """Persist an updated applied-state checkpoint."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / APPLIED_STATE_FILE, applied_state.to_dict())


def write_ownership_ledger(state_dir: Path, ownership_ledger: OwnershipLedger) -> None:
    """Persist an updated ownership ledger."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / OWNERSHIP_LEDGER_FILE, ownership_ledger.to_dict())


def load_inbox_event_log(state_dir: Path) -> InboxEventLog:
    loaded = _load_optional_document(state_dir / INBOX_EVENTS_FILE, InboxEventLog.from_dict)
    if loaded is None:
        return InboxEventLog(format_version=STATE_FORMAT_VERSION, events=())
    return loaded


def write_inbox_event_log(state_dir: Path, inbox_event_log: InboxEventLog) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / INBOX_EVENTS_FILE, inbox_event_log.to_dict())


def load_job_queue_state(state_dir: Path) -> JobQueueState:
    loaded = _load_optional_document(state_dir / JOB_QUEUE_FILE, JobQueueState.from_dict)
    if loaded is None:
        return JobQueueState(format_version=STATE_FORMAT_VERSION, jobs=(), foreground_streak=0)
    return loaded


def write_job_queue_state(state_dir: Path, job_queue_state: JobQueueState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / JOB_QUEUE_FILE, job_queue_state.to_dict())


def load_outbound_delivery_log(state_dir: Path) -> OutboundDeliveryLog:
    loaded = _load_optional_document(state_dir / OUTBOUND_DELIVERIES_FILE, OutboundDeliveryLog.from_dict)
    if loaded is None:
        return OutboundDeliveryLog(format_version=STATE_FORMAT_VERSION, records=())
    return loaded


def write_outbound_delivery_log(state_dir: Path, delivery_log: OutboundDeliveryLog) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / OUTBOUND_DELIVERIES_FILE, delivery_log.to_dict())


def clear_state_documents(state_dir: Path) -> None:
    """Remove all persisted state documents together after full teardown."""

    for file_name in STATE_DOCUMENT_FILES:
        document_path = state_dir / file_name
        if document_path.exists():
            document_path.unlink()


def _load_optional_document(
    path: Path,
    loader: Callable[[dict[str, Any]], _DocumentT],
) -> _DocumentT | None:
    if not path.exists():
        return None
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        msg = f"State file '{path.name}' must contain a JSON object."
        raise StateValidationError(msg)
    try:
        return loader(payload)
    except StateValidationError as error:
        msg = f"Invalid state file '{path.name}': {error}"
        raise StateValidationError(msg) from error


def _read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        msg = f"State file '{path.name}' contains invalid JSON: {error.msg}."
        raise StateValidationError(msg) from error


def _write_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _generate_secret(*, prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def _queue_policy_module() -> Any:
    return import_module("dokploy_wizard.state.queue_policy")


def _normalize_queue_name(queue: str) -> str:
    if queue not in {"foreground", "background"}:
        msg = f"Unsupported queue lane '{queue}'."
        raise StateValidationError(msg)
    return queue


def _stable_identity(prefix: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _as_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(tz=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str:
    return _as_datetime(value).isoformat()
