# mypy: ignore-errors
"""Durable inbox and queue models for narrow runtime workflows."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dokploy_wizard.state.models import STATE_FORMAT_VERSION, StateValidationError

JOB_STATUS_VALUES = {
    "queued",
    "leased",
    "retry",
    "completed",
    "dead_letter",
    "superseded",
}
JOB_LANE_VALUES = {"foreground", "background"}
DELIVERY_STATUS_VALUES = {"pending", "sent"}
DELIVERY_THREAD_MODE_VALUES = {"room", "thread"}


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        msg = f"Expected integer for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        msg = f"Expected non-empty string for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        msg = f"Expected non-empty string or null for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_format_version(payload: dict[str, Any]) -> int:
    version = _require_int(payload, "format_version")
    if version != STATE_FORMAT_VERSION:
        msg = f"Unsupported format_version {version}; expected {STATE_FORMAT_VERSION}."
        raise StateValidationError(msg)
    return version


def _require_json_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        msg = f"Expected object for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_timestamp(payload: dict[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    _validate_timestamp(value, key=key)
    return value


def _require_optional_timestamp(payload: dict[str, Any], key: str) -> str | None:
    value = _require_optional_string(payload, key)
    if value is None:
        return None
    _validate_timestamp(value, key=key)
    return value


def _validate_timestamp(value: str, *, key: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        msg = f"Expected ISO-8601 timestamp for '{key}'."
        raise StateValidationError(msg) from error
    if parsed.tzinfo is None:
        msg = f"Expected timezone-aware timestamp for '{key}'."
        raise StateValidationError(msg)


@dataclass(frozen=True)
class InboxEventRecord:
    """Durable audit record for a received ingress event."""

    format_version: int
    event_id: str
    source: str
    idempotency_key: str
    received_at: str
    raw_body: bytes
    parsed_payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if self.event_id == "" or self.source == "" or self.idempotency_key == "":
            msg = "Inbox event identity fields must be non-empty strings."
            raise StateValidationError(msg)
        _validate_timestamp(self.received_at, key="received_at")
        if not isinstance(self.raw_body, bytes):
            msg = "Inbox event raw body must be bytes."
            raise StateValidationError(msg)
        if not isinstance(self.parsed_payload, dict):
            msg = "Inbox event parsed payload must be an object."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "event_id": self.event_id,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "received_at": self.received_at,
            "raw_body_base64": base64.b64encode(self.raw_body).decode("ascii"),
            "parsed_payload": self.parsed_payload,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InboxEventRecord:
        raw_body_base64 = _require_string(payload, "raw_body_base64")
        try:
            raw_body = base64.b64decode(raw_body_base64.encode("ascii"), validate=True)
        except ValueError as error:
            msg = "Expected valid base64 string for 'raw_body_base64'."
            raise StateValidationError(msg) from error
        return cls(
            format_version=_require_format_version(payload),
            event_id=_require_string(payload, "event_id"),
            source=_require_string(payload, "source"),
            idempotency_key=_require_string(payload, "idempotency_key"),
            received_at=_require_timestamp(payload, "received_at"),
            raw_body=raw_body,
            parsed_payload=_require_json_object(payload, "parsed_payload"),
        )


@dataclass(frozen=True)
class InboxEventLog:
    """Durable collection of inbox events."""

    format_version: int
    events: tuple[InboxEventRecord, ...]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        seen: set[tuple[str, str]] = set()
        for event in self.events:
            identity = (event.source, event.idempotency_key)
            if identity in seen:
                msg = (
                    "Inbox event log contains duplicate source/idempotency pair "
                    f"{event.source}:{event.idempotency_key}."
                )
                raise StateValidationError(msg)
            seen.add(identity)

    def to_dict(self) -> dict[str, Any]:
        ordered_events = sorted(self.events, key=lambda item: (item.received_at, item.event_id))
        return {
            "format_version": self.format_version,
            "events": [event.to_dict() for event in ordered_events],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InboxEventLog:
        events_payload = payload.get("events")
        if not isinstance(events_payload, list):
            msg = "Expected list for 'events'."
            raise StateValidationError(msg)
        events = tuple(
            InboxEventRecord.from_dict(item) for item in events_payload if isinstance(item, dict)
        )
        if len(events) != len(events_payload):
            msg = "Each inbox event must be an object."
            raise StateValidationError(msg)
        return cls(format_version=_require_format_version(payload), events=events)


@dataclass(frozen=True)
class DurableJobRecord:
    """Durable executable work item with explicit scheduling metadata."""

    format_version: int
    job_id: str
    kind: str
    scope_key: str
    supersession_key: str | None
    lane: str
    status: str
    attempt_count: int
    max_attempts: int
    run_after: str
    lease_owner: str | None
    leased_until: str | None
    created_at: str
    updated_at: str
    last_error: str | None
    last_error_at: str | None
    idempotency_key: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if self.job_id == "" or self.kind == "" or self.scope_key == "":
            msg = "Job identity fields must be non-empty strings."
            raise StateValidationError(msg)
        if self.supersession_key == "":
            msg = "Job supersession key must be omitted or a non-empty string."
            raise StateValidationError(msg)
        if self.lane not in JOB_LANE_VALUES:
            msg = f"Unsupported job lane '{self.lane}'."
            raise StateValidationError(msg)
        if self.status not in JOB_STATUS_VALUES:
            msg = f"Unsupported job status '{self.status}'."
            raise StateValidationError(msg)
        if self.attempt_count < 0:
            msg = "Job attempt count cannot be negative."
            raise StateValidationError(msg)
        if self.max_attempts < 1:
            msg = "Job max attempts must be at least 1."
            raise StateValidationError(msg)
        if self.attempt_count > self.max_attempts:
            msg = "Job attempt count cannot exceed max attempts."
            raise StateValidationError(msg)
        _validate_timestamp(self.run_after, key="run_after")
        _validate_timestamp(self.created_at, key="created_at")
        _validate_timestamp(self.updated_at, key="updated_at")
        if self.last_error_at is not None:
            _validate_timestamp(self.last_error_at, key="last_error_at")
        if self.status == "leased":
            if self.lease_owner is None or self.leased_until is None:
                msg = "Leased jobs require lease owner and leased-until timestamps."
                raise StateValidationError(msg)
        elif self.lease_owner is not None or self.leased_until is not None:
            msg = "Only leased jobs may carry lease fields."
            raise StateValidationError(msg)
        if self.leased_until is not None:
            _validate_timestamp(self.leased_until, key="leased_until")
        if self.idempotency_key == "":
            msg = "Job idempotency key must be a non-empty string."
            raise StateValidationError(msg)
        if not isinstance(self.payload, dict):
            msg = "Job payload must be an object."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "job_id": self.job_id,
            "kind": self.kind,
            "scope_key": self.scope_key,
            "supersession_key": self.supersession_key,
            "lane": self.lane,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "run_after": self.run_after,
            "lease_owner": self.lease_owner,
            "leased_until": self.leased_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DurableJobRecord:
        return cls(
            format_version=_require_format_version(payload),
            job_id=_require_string(payload, "job_id"),
            kind=_require_string(payload, "kind"),
            scope_key=_require_string(payload, "scope_key"),
            supersession_key=_require_optional_string(payload, "supersession_key"),
            lane=_require_string(payload, "lane"),
            status=_require_string(payload, "status"),
            attempt_count=_require_int(payload, "attempt_count"),
            max_attempts=_require_int(payload, "max_attempts"),
            run_after=_require_timestamp(payload, "run_after"),
            lease_owner=_require_optional_string(payload, "lease_owner"),
            leased_until=_require_optional_timestamp(payload, "leased_until"),
            created_at=_require_timestamp(payload, "created_at"),
            updated_at=_require_timestamp(payload, "updated_at"),
            last_error=_require_optional_string(payload, "last_error"),
            last_error_at=_require_optional_timestamp(payload, "last_error_at"),
            idempotency_key=_require_string(payload, "idempotency_key"),
            payload=_require_json_object(payload, "payload"),
        )


@dataclass(frozen=True)
class OutboundDeliveryRecord:
    """Durable outbound send record used to suppress duplicate visible replies."""

    format_version: int
    delivery_id: str
    channel: str
    delivery_key: str
    transport: str
    scope_key: str
    conversation_id: str
    reply_to_message_id: str
    thread_mode: str
    thread_id: str | None
    status: str
    created_at: str
    updated_at: str
    remote_message_id: str | None
    remote_request_id: str | None
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        required_values = (
            self.delivery_id,
            self.channel,
            self.delivery_key,
            self.transport,
            self.scope_key,
            self.conversation_id,
            self.reply_to_message_id,
        )
        if any(value == "" for value in required_values):
            msg = "Outbound delivery identity fields must be non-empty strings."
            raise StateValidationError(msg)
        if self.thread_mode not in DELIVERY_THREAD_MODE_VALUES:
            msg = f"Unsupported outbound delivery thread mode '{self.thread_mode}'."
            raise StateValidationError(msg)
        if self.thread_mode == "thread" and self.thread_id is None:
            msg = "Thread-mode outbound deliveries require a thread id."
            raise StateValidationError(msg)
        if self.thread_mode == "room" and self.thread_id is not None:
            msg = "Room-mode outbound deliveries must not carry a thread id."
            raise StateValidationError(msg)
        if self.status not in DELIVERY_STATUS_VALUES:
            msg = f"Unsupported outbound delivery status '{self.status}'."
            raise StateValidationError(msg)
        if self.status == "sent" and self.remote_message_id is None:
            msg = "Sent outbound deliveries require a remote_message_id."
            raise StateValidationError(msg)
        _validate_timestamp(self.created_at, key="created_at")
        _validate_timestamp(self.updated_at, key="updated_at")
        if not isinstance(self.payload, dict):
            msg = "Outbound delivery payload must be an object."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "delivery_id": self.delivery_id,
            "channel": self.channel,
            "delivery_key": self.delivery_key,
            "transport": self.transport,
            "scope_key": self.scope_key,
            "conversation_id": self.conversation_id,
            "reply_to_message_id": self.reply_to_message_id,
            "thread_mode": self.thread_mode,
            "thread_id": self.thread_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "remote_message_id": self.remote_message_id,
            "remote_request_id": self.remote_request_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutboundDeliveryRecord:
        return cls(
            format_version=_require_format_version(payload),
            delivery_id=_require_string(payload, "delivery_id"),
            channel=_require_string(payload, "channel"),
            delivery_key=_require_string(payload, "delivery_key"),
            transport=_require_string(payload, "transport"),
            scope_key=_require_string(payload, "scope_key"),
            conversation_id=_require_string(payload, "conversation_id"),
            reply_to_message_id=_require_string(payload, "reply_to_message_id"),
            thread_mode=_require_string(payload, "thread_mode"),
            thread_id=_require_optional_string(payload, "thread_id"),
            status=_require_string(payload, "status"),
            created_at=_require_timestamp(payload, "created_at"),
            updated_at=_require_timestamp(payload, "updated_at"),
            remote_message_id=_require_optional_string(payload, "remote_message_id"),
            remote_request_id=_require_optional_string(payload, "remote_request_id"),
            payload=_require_json_object(payload, "payload"),
        )


@dataclass(frozen=True)
class OutboundDeliveryLog:
    """Durable collection of outbound delivery records."""

    format_version: int
    records: tuple[OutboundDeliveryRecord, ...]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        seen: set[tuple[str, str]] = set()
        for record in self.records:
            identity = (record.channel, record.delivery_key)
            if identity in seen:
                msg = (
                    "Outbound delivery log contains duplicate channel/delivery pair "
                    f"{record.channel}:{record.delivery_key}."
                )
                raise StateValidationError(msg)
            seen.add(identity)

    def to_dict(self) -> dict[str, Any]:
        ordered_records = sorted(self.records, key=lambda item: (item.created_at, item.delivery_id))
        return {
            "format_version": self.format_version,
            "records": [record.to_dict() for record in ordered_records],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutboundDeliveryLog:
        records_payload = payload.get("records")
        if not isinstance(records_payload, list):
            msg = "Expected list for 'records'."
            raise StateValidationError(msg)
        records = tuple(
            OutboundDeliveryRecord.from_dict(item)
            for item in records_payload
            if isinstance(item, dict)
        )
        if len(records) != len(records_payload):
            msg = "Each outbound delivery record must be an object."
            raise StateValidationError(msg)
        return cls(format_version=_require_format_version(payload), records=records)


@dataclass(frozen=True)
class JobQueueState:
    """Durable queue plus the minimal fairness cursor required for leasing."""

    format_version: int
    jobs: tuple[DurableJobRecord, ...]
    foreground_streak: int

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if self.foreground_streak < 0:
            msg = "Foreground streak cannot be negative."
            raise StateValidationError(msg)
        seen_job_ids: set[str] = set()
        seen_idempotency_keys: set[str] = set()
        for job in self.jobs:
            if job.job_id in seen_job_ids:
                msg = f"Job queue contains duplicate job id '{job.job_id}'."
                raise StateValidationError(msg)
            seen_job_ids.add(job.job_id)
            if job.idempotency_key in seen_idempotency_keys:
                msg = (
                    "Job queue contains duplicate idempotency key "
                    f"'{job.idempotency_key}'."
                )
                raise StateValidationError(msg)
            seen_idempotency_keys.add(job.idempotency_key)

    def to_dict(self) -> dict[str, Any]:
        ordered_jobs = sorted(self.jobs, key=lambda item: (item.created_at, item.job_id))
        return {
            "format_version": self.format_version,
            "jobs": [job.to_dict() for job in ordered_jobs],
            "foreground_streak": self.foreground_streak,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobQueueState:
        jobs_payload = payload.get("jobs")
        if not isinstance(jobs_payload, list):
            msg = "Expected list for 'jobs'."
            raise StateValidationError(msg)
        jobs = tuple(
            DurableJobRecord.from_dict(item) for item in jobs_payload if isinstance(item, dict)
        )
        if len(jobs) != len(jobs_payload):
            msg = "Each job record must be an object."
            raise StateValidationError(msg)
        return cls(
            format_version=_require_format_version(payload),
            jobs=jobs,
            foreground_streak=_require_int(payload, "foreground_streak"),
        )
