"""Exact Linux process identity checks for workspace refresh actions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol, assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    ProcessIdentity,
    RuntimeIdentityError,
    TransactionRecord,
)


class ProcessControl(Protocol):
    """Controls only processes owned by one transaction generation."""

    def stop(self, pid: int) -> None:
        """Stop the supplied transaction-owned process and wait for it to exit."""

    def start(self, previous: ProcessIdentity) -> int:
        """Start the replacement process and return its observed PID."""

    def replacement_pid(self, previous: ProcessIdentity) -> int | None:
        """Return the already-started replacement PID for a durable start intent."""

    def verify_health(self) -> None:
        """Raise a typed workspace error when the restarted process is unhealthy."""


def require_process_control(control: ProcessControl | None) -> ProcessControl:
    """Require recovery callers to supply explicit process-control capability."""
    if control is None:
        raise RuntimeIdentityError("workspace process recovery needs a process controller")
    return control


def observed_process_identity(*, name: str, pid: int, generation: int) -> ProcessIdentity:
    if pid < 1 or generation < 1:
        raise RuntimeIdentityError("workspace process identity is invalid")
    try:
        return _read_process_identity(name=name, pid=pid, generation=generation)
    except OSError as error:
        raise RuntimeIdentityError("workspace process identity is unreadable") from error


def _read_process_identity(*, name: str, pid: int, generation: int) -> ProcessIdentity:
    proc = Path("/proc") / str(pid)
    stat_bytes = (proc / "stat").read_bytes()
    argv = (proc / "cmdline").read_bytes()
    executable = (proc / "exe").resolve(strict=True).read_bytes()
    try:
        tail = stat_bytes.decode("ascii").rsplit(") ", 1)[1].split()
        start_ticks = int(tail[19])
    except (IndexError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeIdentityError("workspace process stat identity is invalid") from error
    return ProcessIdentity(
        name=name,
        pid=pid,
        start_time_ticks=start_ticks,
        argv_sha256=_sha(argv),
        executable_sha256=_sha(executable),
        generation=generation,
    )


def signal_exact_process(expected: ProcessIdentity, signal: Callable[[int], None]) -> None:
    _require_exact_process(expected)
    signal(expected.pid)


def exact_process_is_running(expected: ProcessIdentity) -> bool:
    """Return false only when the exact durable PID no longer exists."""
    try:
        observed = _read_process_identity(
            name=expected.name,
            pid=expected.pid,
            generation=expected.generation,
        )
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeIdentityError("workspace process identity is unreadable") from error
    _require_same_identity(expected, observed)
    return True


def observe_replacement_process(
    previous: ProcessIdentity, *, pid: int
) -> ProcessIdentity:
    """Observe a new PID instead of trusting the starter's claimed identity."""
    replacement = observed_process_identity(
        name=previous.name, pid=pid, generation=previous.generation
    )
    if (
        replacement.argv_sha256 != previous.argv_sha256
        or replacement.executable_sha256 != previous.executable_sha256
        or (
            replacement.pid == previous.pid
            and replacement.start_time_ticks == previous.start_time_ticks
        )
    ):
        raise RuntimeIdentityError("workspace replacement process identity does not match")
    return replacement


def stop_record_processes(
    record: TransactionRecord,
    *,
    persist: Callable[[TransactionRecord, TransactionRecord], TransactionRecord],
    control: ProcessControl,
) -> TransactionRecord:
    """Persist stop intents, identity-check signals, and terminal stopped status."""
    from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions

    for index, process in enumerate(record.processes):
        match process.status:
            case "observed":
                record = persist(
                    record, transitions.process_status(record, index, "stop_intent")
                )
                process = record.processes[index]
            case "stop_intent":
                pass
            case "stopped":
                continue
            case "start_intent" | "started" | "verified":
                raise RuntimeIdentityError("workspace process stop state is invalid")
            case unreachable:
                assert_never(unreachable)
        if exact_process_is_running(process):
            signal_exact_process(process, control.stop)
        record = persist(record, transitions.process_status(record, index, "stopped"))
    return persist(record, replace(record, phase="processes_stopped"))


def start_record_processes(
    record: TransactionRecord,
    *,
    persist: Callable[[TransactionRecord, TransactionRecord], TransactionRecord],
    control: ProcessControl,
) -> TransactionRecord:
    """Persist start intents and independently observed replacements."""
    from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions

    for index, process in enumerate(record.processes):
        match process.status:
            case "stopped":
                record = persist(
                    record, transitions.process_status(record, index, "start_intent")
                )
                process = record.processes[index]
            case "start_intent":
                pass
            case "started":
                continue
            case "observed" | "stop_intent" | "verified":
                raise RuntimeIdentityError("workspace process start state is invalid")
            case unreachable:
                assert_never(unreachable)
        pid = control.replacement_pid(process)
        if pid is None:
            pid = control.start(process)
        replacement = replace(
            observe_replacement_process(process, pid=pid), status="started"
        )
        record = persist(record, transitions.replace_process(record, index, replacement))
    return persist(record, replace(record, phase="processes_started"))


def verify_record_processes(
    record: TransactionRecord,
    *,
    persist: Callable[[TransactionRecord, TransactionRecord], TransactionRecord],
    control: ProcessControl,
) -> TransactionRecord:
    """Verify health and persist every verified process before health phase."""
    from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions

    control.verify_health()
    for index in range(len(record.processes)):
        record = persist(record, transitions.process_status(record, index, "verified"))
    return persist(record, replace(record, phase="health_verified"))


def stop_started_replacements(record: TransactionRecord, control: ProcessControl) -> None:
    """Stop only exact transaction-owned replacement processes before rollback."""
    for process in record.processes:
        match process.status:
            case "started" | "verified":
                if exact_process_is_running(process):
                    signal_exact_process(process, control.stop)
            case "start_intent":
                pid = control.replacement_pid(process)
                if pid is not None:
                    signal_exact_process(
                        observe_replacement_process(process, pid=pid), control.stop
                    )
            case "observed" | "stop_intent" | "stopped":
                continue
            case unreachable:
                assert_never(unreachable)


def restart_rollback_processes(record: TransactionRecord, control: ProcessControl) -> None:
    """Restore a healthy process set after rollback has restored its files."""
    for process in record.processes:
        match process.status:
            case "observed" | "stop_intent":
                if exact_process_is_running(process):
                    continue
                observe_replacement_process(process, pid=control.start(process))
            case "stopped" | "start_intent" | "started" | "verified":
                observe_replacement_process(process, pid=control.start(process))
            case unreachable:
                assert_never(unreachable)
    control.verify_health()


def _require_exact_process(expected: ProcessIdentity) -> None:
    observed = observed_process_identity(
        name=expected.name,
        pid=expected.pid,
        generation=expected.generation,
    )
    _require_same_identity(expected, observed)


def _require_same_identity(expected: ProcessIdentity, observed: ProcessIdentity) -> None:
    observed_identity = (
        observed.name,
        observed.pid,
        observed.start_time_ticks,
        observed.argv_sha256,
        observed.executable_sha256,
        observed.generation,
    )
    expected_identity = (
        expected.name,
        expected.pid,
        expected.start_time_ticks,
        expected.argv_sha256,
        expected.executable_sha256,
        expected.generation,
    )
    if observed_identity != expected_identity:
        raise RuntimeIdentityError("workspace process identity changed before signal")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
