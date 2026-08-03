from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.workspace_catalog_sync import (
    CatalogTarget,
    ProcessIdentity,
    WorkspaceCatalogTransaction,
    observed_process_identity,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_io import write_record_cas
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    ProcessStatus,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import signal_exact_process
from dokploy_wizard.dokploy.workspace_catalog_sync_transition import (
    assert_transition,
    process_status,
)


def _target(path: Path, content: bytes) -> CatalogTarget:
    return CatalogTarget.file(path=path, content=content, mode=0o640)


def test_generation_commit_checks_all_targets_before_first_write(tmp_path: Path) -> None:
    # Given
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=21)
    prepared = transaction.prepare(
        targets=(_target(first, b"first-managed"), _target(second, b"second-managed")),
        explicit_operator_update=True,
    )
    second.write_bytes(b"second-user-edit")

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    assert first.read_bytes() == b"first-before"
    assert second.read_bytes() == b"second-user-edit"


def test_write_record_cas_rejects_illegal_disk_successor_with_matching_token(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=25)
    prepared = transaction.prepare(targets=(_target(target, b"managed"),))
    written = replace(
        prepared.targets[0], status="written", post_sha256=prepared.targets[0].staged_sha256
    )
    illegal = replace(prepared, phase="health_verified", targets=(written,))

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="transition"):
        write_record_cas(
            transaction.transaction_dir / "transaction.json", expected=prepared, record=illegal
        )
    assert transaction.current() == prepared


def test_write_record_cas_rejects_stale_or_tampered_predecessor(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=26)
    prepared = transaction.prepare(targets=(_target(target, b"managed"),))
    with pytest.raises(RuntimeError, match="injected crash"):
        transaction.commit(cas_token=prepared.cas_token, crash_after="files_written")
    files_written = transaction.current()
    stale = replace(prepared, cas_token=files_written.cas_token)
    tampered = replace(files_written, catalog_sha256="a" * 64)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="predecessor"):
        write_record_cas(
            transaction.transaction_dir / "transaction.json", expected=stale, record=tampered
        )
    assert transaction.current() == files_written


@pytest.mark.parametrize(
    ("initial", "next_status"), [("observed", "verified"), ("stop_intent", "stop_intent")]
)
def test_process_transition_rejects_skipped_or_repeated_intent(
    tmp_path: Path, initial: ProcessStatus, next_status: ProcessStatus
) -> None:
    # Given
    target = tmp_path / "config.json"
    observed = ProcessIdentity(
        name="hermes",
        pid=42,
        start_time_ticks=7,
        argv_sha256="a" * 64,
        executable_sha256="b" * 64,
        generation=27,
        status="observed",
    )
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=27)
    prepared = transaction.prepare(targets=(_target(target, b"managed"),), processes=(observed,))
    process = replace(observed, status=initial)
    record = replace(prepared, processes=(process,))

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="process transition"):
        if initial == "observed":
            successor = replace(record, processes=(replace(process, status=next_status),))
            assert_transition(record, successor)
        else:
            process_status(record, 0, next_status)


class _ProcessController:
    def __init__(self) -> None:
        self._processes: dict[int, subprocess.Popen[str]] = {}

    def observe(self, *, name: str, generation: int) -> ProcessIdentity:
        process = self._spawn()
        return observed_process_identity(name=name, pid=process.pid, generation=generation)

    def stop(self, pid: int) -> None:
        process = self._processes.pop(pid)
        os.kill(pid, signal.SIGTERM)
        process.wait(timeout=5)

    def start(self, previous: ProcessIdentity) -> int:
        del previous
        return self._spawn().pid

    def replacement_pid(self, previous: ProcessIdentity) -> int | None:
        del previous
        running = [pid for pid, process in self._processes.items() if process.poll() is None]
        if len(running) == 1:
            return running[0]
        return None

    def verify_health(self) -> None:
        assert all(process.poll() is None for process in self._processes.values())

    def close(self) -> None:
        for process in self._processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self._processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _spawn(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal; print('ready', flush=True); signal.pause()",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        process.stdout.close()
        self._processes[process.pid] = process
        return process


@pytest.fixture
def process_control() -> Generator[_ProcessController, None, None]:
    controller = _ProcessController()
    yield controller
    controller.close()


def test_process_transaction_persists_stop_start_health_lifecycle(
    tmp_path: Path, process_control: _ProcessController
) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=22)
    previous = process_control.observe(name="hermes", generation=22)
    prepared = transaction.prepare(
        targets=(_target(target, b"managed"),), processes=(previous,)
    )
    switched = transaction.commit(cas_token=prepared.cas_token)

    # When
    stopped = transaction.stop_processes(cas_token=switched.cas_token, control=process_control)
    started = transaction.start_processes(cas_token=stopped.cas_token, control=process_control)
    committed = transaction.verify_health(cas_token=started.cas_token, control=process_control)

    # Then
    assert [switched.phase, stopped.phase, started.phase, committed.phase] == [
        "switched",
        "processes_stopped",
        "processes_started",
        "committed",
    ]
    assert [
        stopped.processes[0].status,
        started.processes[0].status,
        committed.processes[0].status,
    ] == ["stopped", "started", "verified"]
    assert started.processes[0].pid != previous.pid
    assert started.processes[0].start_time_ticks != previous.start_time_ticks


@pytest.mark.parametrize("status", ["started", "verified"])
def test_signal_exact_process_ignores_durable_replacement_status(
    process_control: _ProcessController, status: ProcessStatus
) -> None:
    # Given
    observed = process_control.observe(name="hermes", generation=28)
    replacement = replace(observed, status=status)

    # When
    signal_exact_process(replacement, process_control.stop)

    # Then
    assert replacement.pid not in process_control._processes


@pytest.mark.parametrize(
    "crash_after", ["processes_stopped", "processes_started", "health_verified"]
)
def test_process_phase_crash_recovery_commits_replacement_process(
    tmp_path: Path, process_control: _ProcessController, crash_after: str
) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=23)
    previous = process_control.observe(name="kdense", generation=23)
    prepared = transaction.prepare(
        targets=(_target(target, b"managed"),), processes=(previous,)
    )
    switched = transaction.commit(cas_token=prepared.cas_token)

    # When
    with pytest.raises(RuntimeError, match="injected crash"):
        if crash_after == "processes_stopped":
            transaction.stop_processes(
                cas_token=switched.cas_token,
                control=process_control,
                crash_after=crash_after,
            )
        else:
            stopped = transaction.stop_processes(
                cas_token=switched.cas_token, control=process_control
            )
            if crash_after == "processes_started":
                transaction.start_processes(
                    cas_token=stopped.cas_token,
                    control=process_control,
                    crash_after=crash_after,
                )
            else:
                started = transaction.start_processes(
                    cas_token=stopped.cas_token, control=process_control
                )
                transaction.verify_health(
                    cas_token=started.cas_token, control=process_control, crash_after=crash_after
                )
    recovered = transaction.recover(
        cas_token=transaction.current().cas_token, control=process_control
    )

    # Then
    assert recovered.phase == "committed"
    assert recovered.processes[0].pid != previous.pid
    assert target.read_bytes() == b"managed"
    process_control.verify_health()


def test_blocked_conflict_requires_explicit_safe_rollback(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=24)
    prepared = transaction.prepare(
        targets=(_target(target, b"managed"),), explicit_operator_update=True
    )
    target.write_bytes(b"user-conflict")

    # When
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    with pytest.raises(TransactionBlockedError, match="needs resolution"):
        transaction.recover(cas_token=transaction.current().cas_token)
    target.write_bytes(b"before")
    rolled_back = transaction.resolve_blocked(
        cas_token=transaction.current().cas_token, resolution="rollback"
    )

    # Then
    assert rolled_back.phase == "rolled_back"
    assert target.read_bytes() == b"before"
    assert not (transaction.transaction_dir / "preimages" / "0.bin").exists()
    assert not (transaction.transaction_dir / "staged" / "0.bin").exists()
