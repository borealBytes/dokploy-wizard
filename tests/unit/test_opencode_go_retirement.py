from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from dokploy_wizard.litellm.catalog_retirement import (
    CatalogCandidate,
    CatalogTimeline,
    ClockRegressionError,
    advance_timeline,
    mark_deleted,
)


@dataclass(frozen=True, slots=True)
class FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _candidate(*ids: str) -> CatalogCandidate:
    return CatalogCandidate(source_ids=tuple(sorted(ids)), accepted_ids=tuple(sorted(ids)))


def test_exact_full_id_shrink_waits_24_hours_before_confirmation() -> None:
    started = datetime(2026, 7, 1, tzinfo=UTC)
    previous_ids = tuple(f"model-{index}" for index in range(20))
    initial = advance_timeline(
        CatalogTimeline.empty(),
        _candidate(*previous_ids),
        FrozenClock(started),
    )
    shrunk = _candidate(*previous_ids[:-4])

    first = advance_timeline(initial.timeline, shrunk, FrozenClock(started + timedelta(hours=1)))
    early = advance_timeline(
        first.timeline,
        shrunk,
        FrozenClock(started + timedelta(hours=24, minutes=59)),
    )
    confirmed = advance_timeline(first.timeline, shrunk, FrozenClock(started + timedelta(hours=25)))

    assert first.status == "quarantined_anomalous"
    assert first.timeline.anomaly.state == "pending"
    assert early.status == "quarantined_anomalous"
    assert early.timeline == first.timeline
    assert confirmed.status == "accepted"
    assert confirmed.timeline.anomaly.state == "confirmed"
    assert confirmed.timeline.missing[0].first_missing_at == started + timedelta(hours=25)


def test_candidate_equality_uses_full_zen_ids_not_valid_intersection() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a", "b", "c"), FrozenClock(now))
    candidate = CatalogCandidate(source_ids=("a", "b", "c"), accepted_ids=("a", "b"))

    result = advance_timeline(initial.timeline, candidate, FrozenClock(now + timedelta(hours=1)))

    assert result.status == "accepted_with_quarantine"
    assert result.timeline.anomaly.state == "none"
    assert result.timeline.missing == ()


def test_stable_no_change_is_ephemeral_and_preserves_timeline_bytes() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a", "b"), FrozenClock(now))

    result = advance_timeline(
        initial.timeline,
        _candidate("a", "b"),
        FrozenClock(now + timedelta(hours=23)),
    )

    assert result.status == "accepted"
    assert result.durable_transition is False
    assert result.timeline == initial.timeline


def test_retirement_requires_two_absences_and_seven_continuous_days() -> None:
    started = datetime(2026, 7, 1, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a", "b"), FrozenClock(started))
    missing = _candidate("a")

    first = advance_timeline(initial.timeline, missing, FrozenClock(started + timedelta(hours=1)))
    early = advance_timeline(first.timeline, missing, FrozenClock(started + timedelta(hours=24)))
    second = advance_timeline(first.timeline, missing, FrozenClock(started + timedelta(hours=25)))
    eligible = advance_timeline(
        second.timeline,
        missing,
        FrozenClock(started + timedelta(days=7, hours=1)),
    )

    assert first.timeline.missing[0].state == "pending_absence"
    assert early.timeline.missing[0].state == "pending_absence"
    assert second.timeline.missing[0].state == "confirmed_absence"
    assert eligible.timeline.missing[0].state == "eligible_for_delete"


def test_reappearance_resets_absence_timers() -> None:
    started = datetime(2026, 7, 1, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a", "b"), FrozenClock(started))
    absent = advance_timeline(
        initial.timeline,
        _candidate("a"),
        FrozenClock(started + timedelta(days=1)),
    )

    result = advance_timeline(
        absent.timeline,
        _candidate("a", "b"),
        FrozenClock(started + timedelta(days=2)),
    )

    record = result.timeline.missing[0]
    assert record.state == "reappeared"
    assert record.first_missing_at is None
    assert record.second_missing_at is None
    assert record.delete_not_before is None


def test_clock_regression_rejects_without_mutating_previous_timeline() -> None:
    now = datetime(2026, 7, 2, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a"), FrozenClock(now))

    with pytest.raises(ClockRegressionError):
        advance_timeline(initial.timeline, _candidate("a"), FrozenClock(now - timedelta(seconds=1)))

    assert initial.timeline.last_observed_at == now


def test_eligible_model_requires_explicit_deleted_transition() -> None:
    started = datetime(2026, 7, 1, tzinfo=UTC)
    initial = advance_timeline(CatalogTimeline.empty(), _candidate("a", "b"), FrozenClock(started))
    first = advance_timeline(
        initial.timeline,
        _candidate("a"),
        FrozenClock(started + timedelta(hours=1)),
    )
    second = advance_timeline(
        first.timeline,
        _candidate("a"),
        FrozenClock(started + timedelta(hours=25)),
    )
    eligible = advance_timeline(
        second.timeline,
        _candidate("a"),
        FrozenClock(started + timedelta(days=7, hours=1)),
    )

    deleted = mark_deleted(
        eligible.timeline,
        "b",
        FrozenClock(started + timedelta(days=7, hours=2)),
    )

    assert deleted.missing[0].state == "deleted"
