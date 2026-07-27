from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_missing import (
    MissingObservation,
    MissingRecord,
    advance_missing,
)
from dokploy_wizard.litellm.catalog_types import ObservationStatus

AnomalyState = Literal["none", "pending", "quarantined", "confirmed", "cleared"]
class Clock(Protocol):
    def now(self) -> datetime: ...


class ClockRegressionError(ValueError):
    def __init__(self, previous: datetime, observed: datetime) -> None:
        self.previous = previous
        self.observed = observed
        super().__init__(str(self))

    def __str__(self) -> str:
        return "catalog clock regressed below the persisted observation"


class ClockContractError(ValueError):
    def __init__(self, observed: datetime) -> None:
        self.observed = observed
        super().__init__(str(self))

    def __str__(self) -> str:
        return "catalog clock must return an aware UTC datetime"


class RetirementTransitionError(ValueError):
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        super().__init__(f"model {source_id} is not eligible for deletion")


class CandidateContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    source_ids: tuple[str, ...]
    accepted_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise CandidateContractError("candidate source ids must be sorted and unique")
        if self.accepted_ids != tuple(sorted(set(self.accepted_ids))):
            raise CandidateContractError("candidate accepted ids must be sorted and unique")
        if not set(self.accepted_ids).issubset(self.source_ids):
            raise CandidateContractError("candidate accepted ids must be present in source ids")


@dataclass(frozen=True, slots=True)
class AnomalyRecord:
    state: AnomalyState
    candidate_ids_sha256: str | None
    candidate_count: int | None
    previous_count: int | None
    threshold: int | None
    first_seen_at: datetime | None
    confirmed_at: datetime | None

    @classmethod
    def empty(cls) -> AnomalyRecord:
        return cls("none", None, None, None, None, None, None)


@dataclass(frozen=True, slots=True)
class CatalogTimeline:
    accepted_source_ids: tuple[str, ...]
    accepted_ids: tuple[str, ...]
    last_observed_at: datetime | None
    anomaly: AnomalyRecord
    missing: tuple[MissingRecord, ...]

    @classmethod
    def empty(cls) -> CatalogTimeline:
        return cls((), (), None, AnomalyRecord.empty(), ())


@dataclass(frozen=True, slots=True)
class TimelineResult:
    status: ObservationStatus
    timeline: CatalogTimeline
    durable_transition: bool


def advance_timeline(
    previous: CatalogTimeline,
    candidate: CatalogCandidate,
    clock: Clock,
) -> TimelineResult:
    observed_at = clock.now()
    _require_utc(observed_at)
    if previous.last_observed_at is not None and observed_at < previous.last_observed_at:
        raise ClockRegressionError(previous.last_observed_at, observed_at)
    if not previous.accepted_source_ids:
        return _accept(previous, candidate, observed_at, AnomalyRecord.empty())
    if (
        candidate.source_ids == previous.accepted_source_ids
        and candidate.accepted_ids == previous.accepted_ids
        and not previous.missing
        and previous.anomaly.state not in {"pending", "quarantined"}
    ):
        status: ObservationStatus = (
            "accepted_with_quarantine"
            if candidate.accepted_ids != candidate.source_ids
            else "accepted"
        )
        return TimelineResult(status, previous, False)
    removed_count = len(set(previous.accepted_source_ids) - set(candidate.source_ids))
    threshold = max(3, math.ceil(len(previous.accepted_source_ids) * 0.20))
    if removed_count >= threshold:
        return _advance_anomaly(previous, candidate, observed_at, threshold)
    anomaly = _cleared_anomaly(previous.anomaly)
    return _accept(previous, candidate, observed_at, anomaly)


def mark_deleted(
    timeline: CatalogTimeline,
    source_id: str,
    clock: Clock,
) -> CatalogTimeline:
    observed_at = clock.now()
    _require_utc(observed_at)
    if timeline.last_observed_at is not None and observed_at < timeline.last_observed_at:
        raise ClockRegressionError(timeline.last_observed_at, observed_at)
    record = next(
        (
            item
            for item in timeline.missing
            if item.source_id == source_id and item.state == "eligible_for_delete"
        ),
        None,
    )
    if record is None:
        raise RetirementTransitionError(source_id)
    updated = tuple(
        replace(item, state="deleted") if item.source_id == source_id else item
        for item in timeline.missing
    )
    return replace(timeline, missing=updated)


def _advance_anomaly(
    previous: CatalogTimeline,
    candidate: CatalogCandidate,
    observed_at: datetime,
    threshold: int,
) -> TimelineResult:
    candidate_sha = _ids_sha(candidate.source_ids)
    anomaly = previous.anomaly
    same_candidate = (
        anomaly.state in {"pending", "quarantined"}
        and anomaly.candidate_ids_sha256 == candidate_sha
    )
    if not same_candidate:
        pending = AnomalyRecord(
            "pending",
            candidate_sha,
            len(candidate.source_ids),
            len(previous.accepted_source_ids),
            threshold,
            observed_at,
            None,
        )
        timeline = CatalogTimeline(
            previous.accepted_source_ids,
            previous.accepted_ids,
            observed_at,
            pending,
            previous.missing,
        )
        return TimelineResult("quarantined_anomalous", timeline, True)
    if anomaly.first_seen_at is None:
        raise AssertionError("pending anomaly lacks first-seen time")
    if observed_at - anomaly.first_seen_at < timedelta(hours=24):
        return TimelineResult("quarantined_anomalous", previous, False)
    confirmed = AnomalyRecord(
        "confirmed",
        candidate_sha,
        len(candidate.source_ids),
        len(previous.accepted_source_ids),
        threshold,
        anomaly.first_seen_at,
        observed_at,
    )
    return _accept(previous, candidate, observed_at, confirmed)


def _accept(
    previous: CatalogTimeline,
    candidate: CatalogCandidate,
    observed_at: datetime,
    anomaly: AnomalyRecord,
) -> TimelineResult:
    missing = advance_missing(
        MissingObservation(
            previous.accepted_source_ids,
            previous.missing,
            candidate.source_ids,
            observed_at,
            previous.last_observed_at,
        )
    )
    timeline = CatalogTimeline(
        candidate.source_ids,
        candidate.accepted_ids,
        observed_at,
        anomaly,
        missing,
    )
    status: ObservationStatus = (
        "accepted_with_quarantine"
        if candidate.accepted_ids != candidate.source_ids
        else "accepted"
    )
    return TimelineResult(status, timeline, timeline != previous)


def _cleared_anomaly(previous: AnomalyRecord) -> AnomalyRecord:
    if previous.state == "none":
        return previous
    return AnomalyRecord("cleared", None, None, None, None, previous.first_seen_at, None)


def _ids_sha(ids: tuple[str, ...]) -> str:
    return sha256_bytes(canonical_json_bytes(list(ids)))


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ClockContractError(value)
    if value.tzinfo is not UTC:
        raise ClockContractError(value)
