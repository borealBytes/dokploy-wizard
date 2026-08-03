"""Closed transition tables and persisted transaction invariants."""

from __future__ import annotations

from typing import Final, assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    ProcessIdentity,
    TargetReceipt,
    TransactionPhase,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)

PHASE_EDGES: Final = {
    "prepared": frozenset({"files_written", "rollback_started", "blocked"}),
    "files_written": frozenset({"pre_switch_verified", "rollback_started", "blocked"}),
    "pre_switch_verified": frozenset({"switched", "rollback_started", "blocked"}),
    "switched": frozenset({"processes_stopped", "committed", "rollback_started", "blocked"}),
    "processes_stopped": frozenset({"processes_started", "rollback_started", "blocked"}),
    "processes_started": frozenset({"health_verified", "rollback_started", "blocked"}),
    "health_verified": frozenset({"committed", "rollback_started", "blocked"}),
    "rollback_started": frozenset({"rolled_back", "blocked"}),
    "committed": frozenset(),
    "rolled_back": frozenset(),
    "blocked": frozenset(),
}
TARGET_EDGES: Final = {
    "observed": frozenset({"prepared"}),
    "prepared": frozenset({"write_intent", "rolled_back"}),
    "write_intent": frozenset({"written", "rollback_intent", "rolled_back"}),
    "written": frozenset({"rollback_intent", "rolled_back"}),
    "rollback_intent": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
}
PROCESS_EDGES: Final = {
    "observed": frozenset({"stop_intent"}),
    "stop_intent": frozenset({"stopped"}),
    "stopped": frozenset({"start_intent"}),
    "start_intent": frozenset({"started"}),
    "started": frozenset({"verified"}),
    "verified": frozenset(),
}


def assert_transition(previous: TransactionRecord, next_record: TransactionRecord) -> None:
    if (
        previous.generation != next_record.generation
        or previous.created_at != next_record.created_at
        or previous.catalog_sha256 != next_record.catalog_sha256
        or len(previous.targets) != len(next_record.targets)
        or len(previous.processes) != len(next_record.processes)
    ):
        raise WorkspaceCatalogSyncError("workspace transaction immutable fields changed")
    if previous.phase == "blocked":
        if next_record.error is not None or next_record.phase != inferred_blocked_phase(previous):
            raise WorkspaceCatalogSyncError("workspace blocked resolution transition is invalid")
    elif (
        previous.phase != next_record.phase
        and next_record.phase not in PHASE_EDGES[previous.phase]
    ):
        raise WorkspaceCatalogSyncError("workspace transaction phase transition is invalid")
    for previous_target, next_target in zip(
        previous.targets, next_record.targets, strict=True
    ):
        if _target_identity(previous_target) != _target_identity(next_target):
            raise WorkspaceCatalogSyncError("workspace target immutable fields changed")
        if (
            previous_target.status != next_target.status
            and next_target.status not in TARGET_EDGES[previous_target.status]
        ):
            raise WorkspaceCatalogSyncError("workspace target transition is invalid")
    for previous_process, next_process in zip(
        previous.processes, next_record.processes, strict=True
    ):
        if (
            previous_process.status != next_process.status
            and next_process.status not in PROCESS_EDGES[previous_process.status]
        ):
            raise WorkspaceCatalogSyncError("workspace process transition is invalid")
        if next_process.status != "started" and not _same_process_identity(
            previous_process, next_process
        ):
            raise WorkspaceCatalogSyncError("workspace process identity changed before restart")
        if next_process.status == "started" and not _replacement_matches(
            previous_process, next_process
        ):
            raise WorkspaceCatalogSyncError("workspace replacement process identity changed")


def validate_record_invariants(record: TransactionRecord) -> None:
    for target in record.targets:
        _validate_target(target)
    target_statuses = {target.status for target in record.targets}
    process_statuses = {process.status for process in record.processes}
    match record.phase:
        case "blocked":
            valid = record.error is not None
        case "prepared":
            valid = (
                record.error is None
                and target_statuses <= {"prepared", "write_intent", "written"}
                and process_statuses <= {"observed"}
            )
        case "files_written" | "pre_switch_verified" | "switched":
            valid = (
                record.error is None
                and target_statuses == {"written"}
                and process_statuses <= {"observed", "stop_intent", "stopped"}
            )
        case "processes_stopped":
            valid = (
                record.error is None
                and bool(record.processes)
                and target_statuses == {"written"}
                and process_statuses <= {"stopped", "start_intent", "started"}
            )
        case "processes_started":
            valid = (
                record.error is None
                and bool(record.processes)
                and target_statuses == {"written"}
                and process_statuses <= {"started", "verified"}
            )
        case "health_verified" | "committed":
            valid = (
                record.error is None
                and target_statuses == {"written"}
                and (not record.processes or process_statuses == {"verified"})
            )
        case "rollback_started":
            valid = record.error is None and target_statuses <= {
                "prepared", "write_intent", "written", "rollback_intent", "rolled_back"
            }
        case "rolled_back":
            valid = record.error is None and target_statuses == {"rolled_back"}
        case unreachable:
            assert_never(unreachable)
    if not valid:
        raise WorkspaceCatalogSyncError("workspace transaction cross-field invariants are invalid")


def inferred_blocked_phase(record: TransactionRecord) -> TransactionPhase:
    statuses = {process.status for process in record.processes}
    if statuses == {"verified"}:
        return "health_verified"
    if statuses == {"started"}:
        return "processes_started"
    if statuses == {"stopped"}:
        return "processes_stopped"
    if statuses <= {"observed"} and {target.status for target in record.targets} == {"written"}:
        return "files_written"
    if statuses <= {"observed"}:
        return "prepared"
    raise WorkspaceCatalogSyncError("workspace blocked process state needs manual recovery")


def _validate_target(target: TargetReceipt) -> None:
    match target.pre_state:
        case "absent":
            valid = (
                target.pre_sha256 is None
                and target.pre_mode is None
                and target.pre_target is None
            )
        case "file":
            valid = (
                target.pre_sha256 is not None
                and target.pre_mode is not None
                and target.pre_target is None
            )
        case "symlink":
            valid = (
                target.pre_sha256 is not None
                and target.pre_mode is None
                and target.pre_target is not None
            )
        case unreachable:
            assert_never(unreachable)
    if not valid or target.status == "observed":
        raise WorkspaceCatalogSyncError("workspace target preimage is invalid")
    if target.kind == "file":
        mode_valid = target.post_mode is not None and (
            target.pre_state != "file" or target.post_mode == target.pre_mode
        )
    else:
        mode_valid = target.post_mode is None
    if not mode_valid:
        raise WorkspaceCatalogSyncError("workspace target post mode is invalid")
    if target.status in {"prepared", "write_intent"} and target.post_sha256 is not None:
        raise WorkspaceCatalogSyncError("workspace target postimage is invalid")
    if (
        target.status in {"written", "rollback_intent"}
        and target.post_sha256 != target.staged_sha256
    ):
        raise WorkspaceCatalogSyncError("workspace target postimage is invalid")


def _target_identity(
    target: TargetReceipt,
) -> tuple[str, str, str, str | None, str | None, str | None, str | None, str]:
    return (
        target.path,
        target.kind,
        target.pre_state,
        target.pre_sha256,
        target.pre_mode,
        target.pre_target,
        target.post_mode,
        target.staged_sha256,
    )


def _replacement_matches(previous: ProcessIdentity, next_process: ProcessIdentity) -> bool:
    return (
        previous.name == next_process.name
        and previous.argv_sha256 == next_process.argv_sha256
        and previous.executable_sha256 == next_process.executable_sha256
        and previous.generation == next_process.generation
    )


def _same_process_identity(previous: ProcessIdentity, next_process: ProcessIdentity) -> bool:
    return (
        previous.name == next_process.name
        and previous.pid == next_process.pid
        and previous.start_time_ticks == next_process.start_time_ticks
        and _replacement_matches(previous, next_process)
    )
