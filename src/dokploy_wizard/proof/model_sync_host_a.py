"""Host A guard lifecycle helpers for the baseline orchestrator."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.model_sync_env import PreparedEnv, restore_proof_env
from dokploy_wizard.proof.model_sync_state import (
    claim_abort_guard,
    transfer_abort_guard_to_plan,
)


@dataclass(frozen=True, slots=True)
class GuardClaim:
    token: str
    pid: int
    start_time_ticks: str


def claim_plan_guard(*, guard_path: Path, pid: int, start_time_ticks: str) -> GuardClaim:
    """Claim durable plan ownership for this exact process before a guarded proof step."""
    token = secrets.token_urlsafe(24)
    claim_abort_guard(
        guard_path,
        pid=pid,
        start_time_ticks=start_time_ticks,
        claim_token=token,
    )
    return GuardClaim(token=token, pid=pid, start_time_ticks=start_time_ticks)


def restore_after_interrupt(*, prepared: PreparedEnv, guard_path: Path, claim: GuardClaim) -> None:
    """Restore exact bytes first, then return the guard to durable plan ownership."""
    restore_proof_env(prepared=prepared, guard_path=guard_path)
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)


def complete_resumable_step(*, guard_path: Path, claim: GuardClaim) -> None:
    """Return an unchanged proof environment to the durable plan after a successful step."""
    transfer_abort_guard_to_plan(guard_path, claim_token=claim.token)
