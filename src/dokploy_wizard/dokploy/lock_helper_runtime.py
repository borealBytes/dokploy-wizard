"""Self-contained runtime copied into the Shared Core metadata volume."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.lock_helper_io import atomic_json as atomic_json
    from dokploy_wizard.dokploy.lock_helper_io import read_json
    from dokploy_wizard.dokploy.lock_helper_protocol import (
        ProtocolError,
        Receipt,
        Request,
        parse_receipt,
        parse_release,
        parse_request,
        parse_result,
    )
elif __package__:
    from dokploy_wizard.dokploy.lock_helper_io import atomic_json as atomic_json
    from dokploy_wizard.dokploy.lock_helper_io import read_json
    from dokploy_wizard.dokploy.lock_helper_protocol import (
        ProtocolError,
        Receipt,
        Request,
        parse_receipt,
        parse_release,
        parse_request,
        parse_result,
    )
else:
    from lock_helper_io import atomic_json as atomic_json
    from lock_helper_io import read_json
    from lock_helper_protocol import (
        ProtocolError,
        Receipt,
        Request,
        parse_receipt,
        parse_release,
        parse_request,
        parse_result,
    )

_TRANSITIONS = {
    "created": frozenset({"starting", "failed"}),
    "starting": frozenset({"acquired_held", "failed"}),
    "acquired_held": frozenset({"parent_running", "failed"}),
    "parent_running": frozenset({"after_snapshot_written", "failed"}),
    "after_snapshot_written": frozenset({"release_requested", "failed"}),
    "release_requested": frozenset({"released", "failed"}),
}


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _parent_matches(request: Request) -> bool:
    parent = Path("/proc") / str(request.parent_pid)
    try:
        fields = (parent / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        ticks = int(fields[19])
        argv = hashlib.sha256((parent / "cmdline").read_bytes()).hexdigest()
    except (FileNotFoundError, IndexError, UnicodeDecodeError, ValueError):
        return False
    return ticks == request.parent_start_time_ticks and argv == request.parent_argv_sha256


def _transition(
    receipt: Receipt,
    request: Request,
    next_phase: str,
    *,
    lock_inode: int | None = None,
    result_sha256: str | None = None,
    error: str | None = None,
) -> Receipt:
    if next_phase not in _TRANSITIONS.get(receipt.phase, frozenset()):
        raise ProtocolError("receipt transition is invalid")
    now = _now()
    updated = dict(receipt.payload)
    updated.update(
        {
            "error": error,
            "generation": receipt.generation + 1,
            "heartbeat_at": now.isoformat(),
            "heartbeat_deadline_at": (now + timedelta(seconds=5)).isoformat(),
            "lock_inode": lock_inode if lock_inode is not None else updated["lock_inode"],
            "phase": next_phase,
            "receipt_version": receipt.receipt_version + 1,
            "result_sha256": (
                result_sha256 if result_sha256 is not None else updated["result_sha256"]
            ),
        }
    )
    return parse_receipt(updated, request)


def _persist_transition(
    path: Path,
    receipt: Receipt,
    request: Request,
    phase: str,
    *,
    lock_inode: int | None = None,
    result_sha256: str | None = None,
    error: str | None = None,
) -> Receipt:
    advanced = _transition(
        receipt,
        request,
        phase,
        lock_inode=lock_inode,
        result_sha256=result_sha256,
        error=error,
    )
    atomic_json(path, advanced.payload)
    return advanced


def _heartbeat(path: Path, receipt: Receipt, request: Request) -> Receipt:
    now = _now()
    updated = dict(receipt.payload)
    updated["heartbeat_at"] = now.isoformat()
    updated["heartbeat_deadline_at"] = (now + timedelta(seconds=5)).isoformat()
    refreshed = parse_receipt(updated, request)
    atomic_json(path, refreshed.payload)
    return refreshed


def _fail(path: Path, receipt: Receipt, request: Request, error: str) -> int:
    failed = _persist_transition(path, receipt, request, "failed", error=error)
    if failed.phase != "failed":
        return 70
    return 75


def run(request_path: Path, timeout_seconds: int) -> int:
    request = parse_request(read_json(request_path))
    root = request_path.parents[1]
    receipt_path = root / "lease-receipts" / f"{request.lease}.json"
    receipt = parse_receipt(read_json(receipt_path), request)
    if receipt.phase != "created" or receipt.generation < request.generation:
        return 70
    if not _parent_matches(request):
        return _fail(receipt_path, receipt, request, "parent identity mismatch")
    receipt = _persist_transition(receipt_path, receipt, request, "starting")
    descriptor = os.open(root / "sync.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _fail(receipt_path, receipt, request, "lock busy")
        receipt = _persist_transition(
            receipt_path,
            receipt,
            request,
            "acquired_held",
            lock_inode=os.fstat(descriptor).st_ino,
        )
        receipt = _persist_transition(receipt_path, receipt, request, "parent_running")
        deadline = time.monotonic() + timeout_seconds
        heartbeat_due = time.monotonic() + 1
        release_path = root / "lease-releases" / f"{request.lease}.json"
        while time.monotonic() <= deadline:
            if not _parent_matches(request):
                return _fail(receipt_path, receipt, request, "dead parent")
            if release_path.exists():
                result = parse_result(
                    read_json(root / "lease-results" / f"{request.lease}.json"),
                    request,
                    receipt,
                )
                parse_release(read_json(release_path), request, receipt, result)
                receipt = _persist_transition(
                    receipt_path,
                    receipt,
                    request,
                    "after_snapshot_written",
                    result_sha256=result.sha256,
                )
                receipt = _persist_transition(
                    receipt_path,
                    receipt,
                    request,
                    "release_requested",
                )
                _persist_transition(receipt_path, receipt, request, "released")
                return 0
            if time.monotonic() >= heartbeat_due:
                receipt = _heartbeat(receipt_path, receipt, request)
                heartbeat_due = time.monotonic() + 1
            time.sleep(0.05)
        return _fail(receipt_path, receipt, request, "lease timeout")
    except (OSError, json.JSONDecodeError, ProtocolError):
        if receipt.phase in _TRANSITIONS:
            return _fail(receipt_path, receipt, request, "invalid helper IPC")
        return 70
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    args = parser.parse_args()
    try:
        return run(args.request, args.timeout_seconds)
    except (OSError, json.JSONDecodeError, ProtocolError):
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
