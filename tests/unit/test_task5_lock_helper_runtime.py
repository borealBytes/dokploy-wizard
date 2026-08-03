from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from dokploy_wizard.dokploy import lock_helper_runtime
from dokploy_wizard.dokploy.sync_helper import (
    LeaseReceipt,
    LeaseRelease,
    LeaseRequest,
    LeaseResult,
    read_parent_identity,
)
from dokploy_wizard.state.upgrade_io import atomic_json, read_json


def test_lock_helper_holds_flock_until_exact_release_cas(tmp_path: Path) -> None:
    lease = "97099d6d-fd71-4ae0-8779-df6de483dfcc"
    parent = read_parent_identity(os.getpid())
    request = LeaseRequest(
        lease=lease,
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
        created_at="2026-07-28T00:00:00Z",
    )
    request_path = tmp_path / "lease-requests" / f"{lease}.json"
    receipt_path = tmp_path / "lease-receipts" / f"{lease}.json"
    atomic_json(request_path, request.to_dict())
    atomic_json(
        receipt_path,
        replace(
            LeaseReceipt.created(request=request, container_id="f" * 64),
            generation=request.generation + 1,
            receipt_version=request.receipt_version + 1,
        ).to_dict(),
    )
    process = subprocess.Popen(
        (
            sys.executable,
            str(Path(lock_helper_runtime.__file__)),
            "--request",
            str(request_path),
            "--timeout-seconds",
            "5",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    held = _wait_for_phase(receipt_path, "parent_running")
    result = LeaseResult(
        lease=lease,
        generation=held.generation,
        request_sha256=request.sha256(),
        status="succeeded",
        parent_exit_code=0,
        before_snapshot_sha256="6" * 64,
        after_snapshot_sha256="7" * 64,
        parent_reconcile_sha256="8" * 64,
        durable_write_delta=(),
        started_at="2026-07-28T00:00:01Z",
        ended_at="2026-07-28T00:00:02Z",
    )
    atomic_json(tmp_path / "lease-results" / f"{lease}.json", result.to_dict())
    atomic_json(
        tmp_path / "lease-releases" / f"{lease}.json",
        LeaseRelease(
            lease=lease,
            generation=held.generation,
            expected_receipt_version=held.receipt_version,
            expected_result_sha256=result.sha256(),
            requested_at="2026-07-28T00:00:03Z",
        ).to_dict(),
    )

    process.wait(timeout=5)

    assert process.returncode == 0, process.stderr.read() if process.stderr else ""
    assert LeaseReceipt.from_dict(
        read_json(receipt_path, json_values=True)
    ).phase == "released"


def _wait_for_phase(path: Path, phase: str) -> LeaseReceipt:
    deadline = time.monotonic() + 5
    while time.monotonic() <= deadline:
        if path.exists():
            receipt = LeaseReceipt.from_dict(read_json(path, json_values=True))
            if receipt.phase == phase:
                return receipt
        time.sleep(0.02)
    raise AssertionError(f"helper did not reach {phase}")
