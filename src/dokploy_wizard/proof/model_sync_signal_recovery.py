"""Signal deferral helpers for the Task 1 proof command."""

from __future__ import annotations

import signal
from collections.abc import Callable
from types import FrameType

from dokploy_wizard import proof

SignalHandler = Callable[[int, FrameType | None], object] | int | None


def replay_pending_signal(
    recovery: proof.ProofRecovery,
    signal_state: dict[str, bool | int],
    recover_interrupted: Callable[[proof.ProofRecovery], None],
) -> None:
    signum = int(signal_state["pending"])
    if signum:
        recover_interrupted(recovery)
        raise SystemExit(128 + signum)


def install_recovery_handlers(
    recovery: proof.ProofRecovery,
    recover_interrupted: Callable[[proof.ProofRecovery], None],
    signal_state: dict[str, bool | int] | None = None,
) -> tuple[SignalHandler, SignalHandler]:
    if signal_state is None:
        signal_state = {"critical": False, "completed": False, "pending": 0}
    recovering = False

    def restore(signum: int, _frame: FrameType | None) -> None:
        nonlocal recovering
        if signal_state["completed"]:
            recover_interrupted(recovery)
            raise SystemExit(128 + signum)
        if signal_state["critical"]:
            signal_state["pending"] = signum
            return
        if recovering:
            return
        recovering = True
        recover_interrupted(recovery)
        raise SystemExit(128 + signum)

    return (
        signal.signal(signal.SIGINT, restore),
        signal.signal(signal.SIGTERM, restore),
    )


def restore_recovery_handlers(previous: tuple[SignalHandler, SignalHandler]) -> None:
    signal.signal(signal.SIGINT, previous[0])
    signal.signal(signal.SIGTERM, previous[1])
