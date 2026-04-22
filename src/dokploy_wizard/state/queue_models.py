"""Typed durable queue state models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dokploy_wizard.state.models import STATE_FORMAT_VERSION, StateValidationError


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise StateValidationError(f"Expected non-empty string for '{key}'.")
    return value


def _require_optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateValidationError(f"Expected string or null for '{key}'.")
    return value


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise StateValidationError(f"Expected integer for '{key}'.")
    return value


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise StateValidationError(f"Expected object for '{key}'.")
    return value


def _require_format_version(payload: dict[str, Any]) -> int:
    value = _require_int(payload, "format_version")
    if value != STATE_FORMAT_VERSION:
        raise StateValidationError(
            f"Unsupported format_version {value}; expected {STATE_FORMAT_VERSION}."
        )
    return value


@dataclass(frozen=True)
class InboxEventRecord:
    format_version: int
    event_id: str
    source: str
    idempotency_key: str
    received_at: str
    raw_body: bytes
    parsed_payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            raise StateValidationError(
                f"Unsupported format_version {self.format_version}; expected {STATE_FORMAT_VERSION}."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "event_id": self.event_id,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "received_at": self.received_at,
            "raw_body": self.raw_body.decode("utf-8", errors="replace"),
            "parsed_payload": self.parsed_payload,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InboxEventRecord:
        raw_body = payload.get("raw_body", "")
        if not isinstance(raw_body, str):
            raise StateValidationError("Expected string for 'raw_body'.")
        return cls(
            format_version=_require_format_version(payload),
            event_id=_require_string(payload, "event_id"),
            source=_require_string(payload, "source"),
            idempotency_key=_require_string(payload, "idempotency_key"),
            received_at=_require_string(payload, "received_at"),
            raw_body=raw_body.encode("utf-8"),
            parsed_payload=_require_dict(payload, "parsed_payload"),
        )


@dataclass(frozen=True)
class InboxEventLog:
    format_version: int
    events: tuple[InboxEventRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InboxEventLog:
        events = payload.get("events")
        if not isinstance(events, list):
            raise StateValidationError("Expected list for 'events'.")
        return cls(
            format_version=_require_format_version(payload),
            events=tuple(InboxEventRecord.from_dict(item) for item in events if isinstance(item, dict)),
        )


@dataclass(frozen=True)
class DurableJobRecord:
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
            run_after=_require_string(payload, "run_after"),
            lease_owner=_require_optional_string(payload, "lease_owner"),
            leased_until=_require_optional_string(payload, "leased_until"),
            created_at=_require_string(payload, "created_at"),
            updated_at=_require_string(payload, "updated_at"),
            last_error=_require_optional_string(payload, "last_error"),
            last_error_at=_require_optional_string(payload, "last_error_at"),
            idempotency_key=_require_string(payload, "idempotency_key"),
            payload=_require_dict(payload, "payload"),
        )


@dataclass(frozen=True)
class JobQueueState:
    format_version: int
    jobs: tuple[DurableJobRecord, ...]
    foreground_streak: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "jobs": [job.to_dict() for job in self.jobs],
            "foreground_streak": self.foreground_streak,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobQueueState:
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise StateValidationError("Expected list for 'jobs'.")
        return cls(
            format_version=_require_format_version(payload),
            jobs=tuple(DurableJobRecord.from_dict(item) for item in jobs if isinstance(item, dict)),
            foreground_streak=_require_int(payload, "foreground_streak"),
        )


@dataclass(frozen=True)
class OutboundDeliveryRecord:
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
            created_at=_require_string(payload, "created_at"),
            updated_at=_require_string(payload, "updated_at"),
            remote_message_id=_require_optional_string(payload, "remote_message_id"),
            remote_request_id=_require_optional_string(payload, "remote_request_id"),
            payload=_require_dict(payload, "payload"),
        )


@dataclass(frozen=True)
class OutboundDeliveryLog:
    format_version: int
    records: tuple[OutboundDeliveryRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutboundDeliveryLog:
        records = payload.get("records")
        if not isinstance(records, list):
            raise StateValidationError("Expected list for 'records'.")
        return cls(
            format_version=_require_format_version(payload),
            records=tuple(
                OutboundDeliveryRecord.from_dict(item) for item in records if isinstance(item, dict)
            ),
        )
