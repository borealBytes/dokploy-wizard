"""Crash recovery dispatch for durable workspace catalog records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    BlockedResolution,
    TransactionBlockedError,
    TransactionRecord,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import (
    ProcessControl,
    require_process_control,
)


class ProcessAction(Protocol):
    def __call__(
        self,
        *,
        cas_token: str,
        control: ProcessControl,
        crash_after: str | None = None,
    ) -> TransactionRecord: ...


class ResolveAction(Protocol):
    def __call__(
        self,
        *,
        cas_token: str,
        resolution: BlockedResolution,
        control: ProcessControl | None = None,
    ) -> TransactionRecord: ...


class RollbackAction(Protocol):
    def __call__(
        self, record: TransactionRecord, control: ProcessControl | None = None
    ) -> TransactionRecord: ...


class CommitAction(Protocol):
    def __call__(self, record: TransactionRecord) -> TransactionRecord: ...


@dataclass(frozen=True, slots=True)
class RecoveryActions:
    stop: ProcessAction
    start: ProcessAction
    verify: ProcessAction
    rollback: RollbackAction
    commit: CommitAction
    resolve: ResolveAction


def recover_record(
    record: TransactionRecord,
    *,
    control: ProcessControl | None,
    blocked_resolution: BlockedResolution | None,
    actions: RecoveryActions,
) -> TransactionRecord:
    match record.phase:
        case "committed" | "rolled_back":
            return record
        case "prepared" | "files_written" | "pre_switch_verified":
            return actions.rollback(record, control)
        case "switched":
            if any(process.status != "observed" for process in record.processes):
                process_control = require_process_control(control)
                stopped = actions.stop(
                    cas_token=record.cas_token, control=process_control
                )
                started = actions.start(
                    cas_token=stopped.cas_token, control=process_control
                )
                return actions.verify(
                    cas_token=started.cas_token, control=process_control
                )
            return actions.rollback(record, control)
        case "processes_stopped":
            process_control = require_process_control(control)
            started = actions.start(
                cas_token=record.cas_token, control=process_control
            )
            return actions.verify(cas_token=started.cas_token, control=process_control)
        case "processes_started":
            process_control = require_process_control(control)
            return actions.verify(cas_token=record.cas_token, control=process_control)
        case "health_verified":
            return actions.commit(record)
        case "rollback_started":
            return actions.rollback(record, control)
        case "blocked":
            if blocked_resolution is None:
                raise TransactionBlockedError("workspace blocked transaction needs resolution")
            return actions.resolve(
                cas_token=record.cas_token,
                resolution=blocked_resolution,
                control=control,
            )
        case unreachable:
            assert_never(unreachable)
