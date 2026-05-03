# ruff: noqa: E501
"""Narrow queue selection and fairness helpers for runtime workers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from dokploy_wizard.state.queue_models import DurableJobRecord, JobQueueState

FOREGROUND_BURST_LIMIT = 2
_READY_STATUSES = {"queued", "retry"}
_TERMINAL_STATUSES = {"completed", "dead_letter", "superseded"}
_SUPERSEDABLE_STATUSES = {"queued", "retry"}


def sweep_expired_leases(state: JobQueueState, *, now: str) -> JobQueueState:
    """Return a copy with expired leases made runnable again."""

    now_dt = datetime.fromisoformat(now)
    updated_jobs: list[DurableJobRecord] = []
    changed = False
    for job in state.jobs:
        if job.status != "leased" or job.leased_until is None:
            updated_jobs.append(job)
            continue
        if datetime.fromisoformat(job.leased_until) > now_dt:
            updated_jobs.append(job)
            continue
        updated_jobs.append(
            replace(
                job,
                status="retry",
                lease_owner=None,
                leased_until=None,
                updated_at=now,
                last_error="Lease expired before completion.",
                last_error_at=now,
            )
        )
        changed = True
    if not changed:
        return state
    return JobQueueState(
        format_version=state.format_version,
        jobs=tuple(updated_jobs),
        foreground_streak=state.foreground_streak,
    )


def apply_supersession(
    jobs: tuple[DurableJobRecord, ...],
    *,
    scope_key: str,
    supersession_key: str | None,
    now: str,
    replacement_job_id: str,
) -> tuple[DurableJobRecord, ...]:
    """Cancel older queued/retryable jobs replaced by a newer superseding job."""

    if supersession_key is None:
        return jobs

    updated_jobs: list[DurableJobRecord] = []
    for job in jobs:
        if job.status in _TERMINAL_STATUSES:
            updated_jobs.append(job)
            continue
        if job.status not in _SUPERSEDABLE_STATUSES:
            updated_jobs.append(job)
            continue
        if job.scope_key != scope_key or job.supersession_key != supersession_key:
            updated_jobs.append(job)
            continue
        updated_jobs.append(
            replace(
                job,
                status="superseded",
                lease_owner=None,
                leased_until=None,
                updated_at=now,
                last_error=f"Superseded by newer job {replacement_job_id}.",
                last_error_at=now,
            )
        )
    return tuple(updated_jobs)


def choose_next_job(state: JobQueueState, *, now: str) -> DurableJobRecord | None:
    """Choose the next runnable job using explicit scope and fairness rules."""

    active_scopes = _active_scope_keys(state.jobs, now=now)
    if active_scopes:
        return None
    foreground_ready = _ready_jobs(state.jobs, lane="foreground", active_scopes=active_scopes, now=now)
    background_ready = _ready_jobs(state.jobs, lane="background", active_scopes=active_scopes, now=now)

    if foreground_ready and not background_ready:
        return foreground_ready[0]
    if background_ready and not foreground_ready:
        return background_ready[0]
    if not foreground_ready and not background_ready:
        return None
    if state.foreground_streak >= FOREGROUND_BURST_LIMIT:
        return background_ready[0]
    return foreground_ready[0]


def next_foreground_streak(current: int, *, leased_lane: str) -> int:
    """Advance the fairness cursor after a lease."""

    if leased_lane == "foreground":
        return current + 1
    return 0


def _active_scope_keys(jobs: tuple[DurableJobRecord, ...], *, now: str) -> set[str]:
    now_dt = datetime.fromisoformat(now)
    return {
        job.scope_key
        for job in jobs
        if job.status == "leased"
        and job.leased_until is not None
        and datetime.fromisoformat(job.leased_until) > now_dt
    }


def _ready_jobs(
    jobs: tuple[DurableJobRecord, ...],
    *,
    lane: str,
    active_scopes: set[str],
    now: str,
) -> list[DurableJobRecord]:
    now_dt = datetime.fromisoformat(now)
    ready = [
        job
        for job in jobs
        if job.lane == lane
        and job.status in _READY_STATUSES
        and job.scope_key not in active_scopes
        and datetime.fromisoformat(job.run_after) <= now_dt
    ]
    return sorted(ready, key=lambda job: (job.run_after, job.created_at, job.job_id))
