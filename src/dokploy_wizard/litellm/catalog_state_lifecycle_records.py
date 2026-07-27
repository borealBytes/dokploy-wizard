from __future__ import annotations

from datetime import timedelta
from typing import assert_never

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_missing import MissingRecord
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_state_types import LastResult, StateQuarantineRecord
from dokploy_wizard.litellm.catalog_state_validation import (
    StateRecordError,
    anomaly_state,
    digest,
    integer,
    mapping,
    missing_state,
    optional_digest,
    optional_integer,
    optional_timestamp,
    quarantine_reason,
    require_keys,
    result_status,
    text,
    timestamp,
)


def parse_anomaly(value: JsonValue) -> AnomalyRecord:
    data = mapping(value, "anomaly")
    require_keys(
        data,
        {
            "state", "candidate_ids_sha256", "candidate_count", "previous_count",
            "threshold", "first_seen_at", "confirmed_at",
        },
        "anomaly",
    )
    result = AnomalyRecord(
        anomaly_state(text(data["state"], "anomaly state")),
        optional_digest(data["candidate_ids_sha256"]),
        optional_integer(data["candidate_count"], "candidate_count"),
        optional_integer(data["previous_count"], "previous_count"),
        optional_integer(data["threshold"], "threshold"),
        optional_timestamp(data["first_seen_at"], "first_seen_at"),
        optional_timestamp(data["confirmed_at"], "confirmed_at"),
    )
    _validate_anomaly(result)
    return result


def parse_missing(value: JsonValue) -> MissingRecord:
    data = mapping(value, "missing")
    require_keys(
        data,
        {
            "source_id", "state", "last_present_at", "first_missing_at",
            "second_missing_at", "delete_not_before", "observation_sha256",
        },
        "missing",
    )
    result = MissingRecord(
        text(data["source_id"], "source_id"),
        missing_state(text(data["state"], "missing state")),
        optional_timestamp(data["last_present_at"], "last_present_at"),
        optional_timestamp(data["first_missing_at"], "first_missing_at"),
        optional_timestamp(data["second_missing_at"], "second_missing_at"),
        optional_timestamp(data["delete_not_before"], "delete_not_before"),
        digest(data["observation_sha256"]),
    )
    _validate_missing(result)
    return result


def parse_quarantine(value: JsonValue) -> StateQuarantineRecord:
    data = mapping(value, "quarantine")
    require_keys(
        data,
        {
            "source_id", "reason", "first_seen_at", "last_seen_at", "source_sha256",
            "preserved_visible_row_sha256",
        },
        "quarantine",
    )
    result = StateQuarantineRecord(
        text(data["source_id"], "source_id"),
        quarantine_reason(text(data["reason"], "reason")),
        timestamp(data["first_seen_at"], "first_seen_at"),
        timestamp(data["last_seen_at"], "last_seen_at"),
        digest(data["source_sha256"]),
        optional_digest(data["preserved_visible_row_sha256"]),
    )
    if result.last_seen_at < result.first_seen_at:
        raise StateRecordError("quarantine time range is invalid")
    return result


def parse_result(value: JsonValue) -> LastResult:
    data = mapping(value, "last_result")
    require_keys(
        data,
        {
            "status", "reason", "started_at", "ended_at", "input_sha256",
            "output_sha256", "durable_write_delta",
        },
        "last_result",
    )
    result = LastResult(
        result_status(text(data["status"], "result status")),
        text(data["reason"], "reason"),
        timestamp(data["started_at"], "started_at"),
        timestamp(data["ended_at"], "ended_at"),
        optional_digest(data["input_sha256"]),
        optional_digest(data["output_sha256"]),
        integer(data["durable_write_delta"], "durable_write_delta"),
    )
    _validate_result(result)
    return result


def _validate_anomaly(record: AnomalyRecord) -> None:
    candidate = (
        record.candidate_ids_sha256,
        record.candidate_count,
        record.previous_count,
        record.threshold,
    )
    match record.state:
        case "none":
            if (
                candidate != (None, None, None, None)
                or record.first_seen_at is not None
                or record.confirmed_at is not None
            ):
                raise StateRecordError("none anomaly fields are invalid")
        case "cleared":
            if (
                candidate != (None, None, None, None)
                or record.first_seen_at is None
                or record.confirmed_at is not None
            ):
                raise StateRecordError("cleared anomaly fields are invalid")
        case "pending" | "quarantined" | "confirmed":
            if None in candidate or record.first_seen_at is None:
                raise StateRecordError("active anomaly fields are incomplete")
            assert record.candidate_count is not None
            assert record.previous_count is not None
            assert record.threshold is not None
            if (
                record.threshold < 3
                or record.previous_count - record.candidate_count < record.threshold
            ):
                raise StateRecordError("active anomaly threshold is invalid")
            if record.state == "confirmed":
                if record.confirmed_at is None or record.confirmed_at < record.first_seen_at:
                    raise StateRecordError("confirmed anomaly time is invalid")
            elif record.confirmed_at is not None:
                raise StateRecordError("unconfirmed anomaly has confirmation time")
        case unreachable:
            assert_never(unreachable)


def _validate_missing(record: MissingRecord) -> None:
    missing_times = (
        record.first_missing_at,
        record.second_missing_at,
        record.delete_not_before,
    )
    match record.state:
        case "present" | "reappeared":
            if record.last_present_at is None or missing_times != (None, None, None):
                raise StateRecordError("present missing-record fields are invalid")
        case "pending_absence":
            if (
                record.last_present_at is None
                or record.first_missing_at is None
                or missing_times[1:] != (None, None)
            ):
                raise StateRecordError("pending absence fields are invalid")
        case "confirmed_absence" | "eligible_for_delete" | "deleted":
            if record.last_present_at is None or None in missing_times:
                raise StateRecordError("confirmed absence fields are incomplete")
            assert record.first_missing_at is not None
            assert record.second_missing_at is not None
            assert record.delete_not_before is not None
            if record.second_missing_at - record.first_missing_at < timedelta(hours=24):
                raise StateRecordError("second absence time is invalid")
            if record.delete_not_before != record.first_missing_at + timedelta(days=7):
                raise StateRecordError("missing deletion boundary is invalid")
        case unreachable:
            assert_never(unreachable)


def _validate_result(record: LastResult) -> None:
    if record.ended_at < record.started_at or record.durable_write_delta < 0:
        raise StateRecordError("last-result time or write count is invalid")
    match record.status:
        case "success" | "success_with_quarantine":
            if (
                record.input_sha256 is None
                or record.output_sha256 is None
                or record.durable_write_delta != 2
            ):
                raise StateRecordError("successful result fields are invalid")
        case "no_change":
            if (
                record.input_sha256 is None
                or record.output_sha256 is None
                or record.durable_write_delta != 0
            ):
                raise StateRecordError("no-change result fields are invalid")
        case "quarantined":
            if (
                record.input_sha256 is None
                or record.output_sha256 is not None
                or record.durable_write_delta != 0
            ):
                raise StateRecordError("quarantined result fields are invalid")
        case "failed":
            if record.output_sha256 is not None or record.durable_write_delta != 0:
                raise StateRecordError("failed result fields are invalid")
        case unreachable:
            assert_never(unreachable)
