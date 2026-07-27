from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes, sha256_bytes

MissingState = Literal[
    "present",
    "pending_absence",
    "confirmed_absence",
    "eligible_for_delete",
    "deleted",
    "reappeared",
]


@dataclass(frozen=True, slots=True)
class MissingRecord:
    source_id: str
    state: MissingState
    last_present_at: datetime | None
    first_missing_at: datetime | None
    second_missing_at: datetime | None
    delete_not_before: datetime | None
    observation_sha256: str


@dataclass(frozen=True, slots=True)
class MissingObservation:
    previous_source_ids: tuple[str, ...]
    previous_records: tuple[MissingRecord, ...]
    candidate_source_ids: tuple[str, ...]
    observed_at: datetime
    last_observed_at: datetime | None

    @property
    def observation_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(list(self.candidate_source_ids)))


def advance_missing(observation: MissingObservation) -> tuple[MissingRecord, ...]:
    records = {record.source_id: record for record in observation.previous_records}
    observation_sha = observation.observation_sha256
    known_ids = set(observation.previous_source_ids) | set(records)
    for source_id in sorted(known_ids):
        existing = records.get(source_id)
        if source_id in observation.candidate_source_ids:
            if existing is not None:
                records[source_id] = _present_record(
                    existing,
                    observation.observed_at,
                    observation_sha,
                )
            continue
        records[source_id] = _missing_record(
            source_id,
            existing,
            observation,
        )
    return tuple(records[key] for key in sorted(records))


def _missing_record(
    source_id: str,
    existing: MissingRecord | None,
    observation: MissingObservation,
) -> MissingRecord:
    observation_sha = observation.observation_sha256
    if existing is None or existing.state in {"present", "reappeared"}:
        return MissingRecord(
            source_id,
            "pending_absence",
            observation.last_observed_at,
            observation.observed_at,
            None,
            None,
            observation_sha,
        )
    if existing.state == "pending_absence":
        if existing.first_missing_at is None:
            raise AssertionError("pending absence lacks first-missing time")
        if observation.observed_at - existing.first_missing_at < timedelta(hours=24):
            return existing
        return MissingRecord(
            source_id,
            "confirmed_absence",
            existing.last_present_at,
            existing.first_missing_at,
            observation.observed_at,
            existing.first_missing_at + timedelta(days=7),
            observation_sha,
        )
    if existing.state == "confirmed_absence":
        if existing.delete_not_before is None:
            raise AssertionError("confirmed absence lacks deletion boundary")
        if observation.observed_at >= existing.delete_not_before:
            return MissingRecord(
                source_id,
                "eligible_for_delete",
                existing.last_present_at,
                existing.first_missing_at,
                existing.second_missing_at,
                existing.delete_not_before,
                observation_sha,
            )
    return existing


def _present_record(
    existing: MissingRecord,
    observed_at: datetime,
    observation_sha: str,
) -> MissingRecord:
    state: MissingState = (
        "present" if existing.state in {"present", "reappeared"} else "reappeared"
    )
    return MissingRecord(existing.source_id, state, observed_at, None, None, None, observation_sha)
