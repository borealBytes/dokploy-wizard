from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy import lock_helper_runtime
from dokploy_wizard.dokploy.sync_helper import (
    LeaseReceipt,
    LeaseRelease,
    LeaseRequest,
    LeaseResult,
    read_parent_identity,
)
from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_io import atomic_json, read_json

_LEASE = "97099d6d-fd71-4ae0-8779-df6de483dfcc"


def _request(parent_pid: int) -> LeaseRequest:
    parent = read_parent_identity(parent_pid)
    return LeaseRequest(
        lease=_LEASE,
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=parent.pid,
        parent_start_time_ticks=parent.start_time_ticks,
        parent_argv_sha256=parent.argv_sha256,
        env=(("TZ", "2" * 64),),
        input_sha256="3" * 64,
        config_sha256="4" * 64,
        expected_state_sha256="5" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00+00:00",
    )


def _seed(tmp_path: Path, request: LeaseRequest) -> tuple[Path, Path]:
    request_path = tmp_path / "lease-requests" / f"{_LEASE}.json"
    receipt_path = tmp_path / "lease-receipts" / f"{_LEASE}.json"
    atomic_json(request_path, request.to_dict())
    atomic_json(
        receipt_path,
        replace(
            LeaseReceipt.created(request=request, container_id="f" * 64),
            generation=request.generation + 1,
            receipt_version=request.receipt_version + 1,
        ).to_dict(),
    )
    return request_path, receipt_path


def _result(request: LeaseRequest) -> LeaseResult:
    return LeaseResult(
        lease=_LEASE,
        generation=request.generation + 4,
        request_sha256=request.sha256(),
        status="succeeded",
        parent_exit_code=0,
        before_snapshot_sha256="6" * 64,
        after_snapshot_sha256="7" * 64,
        parent_reconcile_sha256="8" * 64,
        durable_write_delta=(),
        started_at="2026-07-28T00:00:01+00:00",
        ended_at="2026-07-28T00:00:02+00:00",
    )


def test_helper_persists_each_success_phase_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(os.getpid())
    request_path, receipt_path = _seed(tmp_path, request)
    result = _result(request)
    atomic_json(tmp_path / "lease-results" / f"{_LEASE}.json", result.to_dict())
    atomic_json(
        tmp_path / "lease-releases" / f"{_LEASE}.json",
        LeaseRelease(
            lease=_LEASE,
            generation=5,
            expected_receipt_version=5,
            expected_result_sha256=result.sha256(),
            requested_at="2026-07-28T00:00:03+00:00",
        ).to_dict(),
    )
    phases: list[str] = []
    write = lock_helper_runtime.atomic_json

    def capture(path: Path, payload: dict[str, JsonValue]) -> None:
        if path == receipt_path:
            phase = payload.get("phase")
            if isinstance(phase, str):
                phases.append(phase)
        write(path, payload)

    monkeypatch.setattr(lock_helper_runtime, "atomic_json", capture)

    assert lock_helper_runtime.run(request_path, 2) == 0
    assert phases == [
        "starting",
        "acquired_held",
        "parent_running",
        "after_snapshot_written",
        "release_requested",
        "released",
    ]


@pytest.mark.parametrize("mismatch", ["lease", "generation", "result"])
def test_helper_rejects_release_or_result_cross_binding(
    tmp_path: Path,
    mismatch: str,
) -> None:
    request = _request(os.getpid())
    request_path, receipt_path = _seed(tmp_path, request)
    result = _result(request)
    expected_hash = result.sha256()
    release_lease = "foreign-lease" if mismatch == "lease" else _LEASE
    generation = 6 if mismatch == "generation" else 5
    if mismatch == "result":
        expected_hash = "0" * 64
    atomic_json(tmp_path / "lease-results" / f"{_LEASE}.json", result.to_dict())
    atomic_json(
        tmp_path / "lease-releases" / f"{_LEASE}.json",
        LeaseRelease(
            lease=release_lease,
            generation=generation,
            expected_receipt_version=5,
            expected_result_sha256=expected_hash,
            requested_at="2026-07-28T00:00:03+00:00",
        ).to_dict(),
    )

    assert lock_helper_runtime.run(request_path, 2) == 75
    receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    assert receipt.phase == "failed"


def test_dead_helper_releases_os_lock(tmp_path: Path) -> None:
    request = _request(os.getpid())
    request_path, receipt_path = _seed(tmp_path, request)
    process = subprocess.Popen(
        (
            sys.executable,
            str(Path(lock_helper_runtime.__file__)),
            "--request",
            str(request_path),
            "--timeout-seconds",
            "10",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_phase(receipt_path, "parent_running")
    process.terminate()
    process.wait(timeout=3)
    descriptor = os.open(tmp_path / "sync.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_parent_death_after_acquisition_persists_failed_receipt(tmp_path: Path) -> None:
    parent = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(0.5)"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    request = _request(parent.pid)
    request_path, receipt_path = _seed(tmp_path, request)
    helper = subprocess.Popen(
        (
            sys.executable,
            str(Path(lock_helper_runtime.__file__)),
            "--request",
            str(request_path),
            "--timeout-seconds",
            "5",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_phase(receipt_path, "parent_running")

    parent.wait(timeout=2)
    helper.wait(timeout=3)

    receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    assert helper.returncode == 75
    assert receipt.phase == "failed"
    assert receipt.error == "dead parent"


@pytest.mark.parametrize("mismatch", ["start_time", "argv"])
def test_helper_rejects_mismatched_parent_identity_tuple(
    tmp_path: Path,
    mismatch: str,
) -> None:
    request = _request(os.getpid())
    bad_request = (
        replace(request, parent_start_time_ticks=request.parent_start_time_ticks + 1)
        if mismatch == "start_time"
        else replace(request, parent_argv_sha256="0" * 64)
    )
    request_path, receipt_path = _seed(tmp_path, bad_request)

    assert lock_helper_runtime.run(request_path, 2) == 75
    receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    assert receipt.phase == "failed"
    assert receipt.error == "parent identity mismatch"


def _wait_for_phase(path: Path, phase: str) -> LeaseReceipt:
    deadline = time.monotonic() + 3
    while time.monotonic() <= deadline:
        receipt = LeaseReceipt.from_dict(read_json(path, json_values=True))
        if receipt.phase == phase:
            return receipt
        time.sleep(0.02)
    raise AssertionError(f"helper did not reach {phase}")
