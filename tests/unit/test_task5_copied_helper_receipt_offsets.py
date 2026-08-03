from __future__ import annotations

import pytest

from dokploy_wizard.dokploy import lock_helper_protocol as protocol

_DIGEST = "a" * 64
_FULL_CONTAINER_ID = "f" * 64
_PHASE_OFFSETS = {
    "created": 1,
    "starting": 2,
    "acquired_held": 3,
    "parent_running": 4,
    "after_snapshot_written": 5,
    "release_requested": 6,
    "released": 7,
}


def _request() -> protocol.Request:
    return protocol.parse_request(
        {
            "config_sha256": _DIGEST,
            "created_at": "2026-07-29T00:00:00+00:00",
            "env": [{"name": "TZ", "value_sha256": _DIGEST}],
            "expected_state_sha256": _DIGEST,
            "generation": 1,
            "input_sha256": _DIGEST,
            "lease": "lease-1",
            "mode": "reconcile",
            "parent_argv_sha256": _DIGEST,
            "parent_pid": 12,
            "parent_start_time_ticks": 34,
            "receipt_version": 1,
            "schema_version": 1,
            "tombstone_sha256": None,
        }
    )


def _receipt(
    request: protocol.Request,
    *,
    phase: str,
    offset: int,
    container_id: str = _FULL_CONTAINER_ID,
    lock_inode: int | None = None,
    result_sha256: str | None = None,
    error: str | None = None,
) -> dict[str, protocol.JsonValue]:
    heartbeat_at = None if phase == "created" else "2026-07-29T00:00:01+00:00"
    heartbeat_deadline = (
        None if phase == "created" else "2026-07-29T00:00:06+00:00"
    )
    return {
        "container_id": container_id,
        "error": error,
        "generation": request.generation + offset,
        "heartbeat_at": heartbeat_at,
        "heartbeat_deadline_at": heartbeat_deadline,
        "lease": request.lease,
        "lock_inode": lock_inode,
        "mode": request.mode,
        "parent_argv_sha256": request.parent_argv_sha256,
        "parent_pid": request.parent_pid,
        "parent_start_time_ticks": request.parent_start_time_ticks,
        "phase": phase,
        "receipt_version": request.receipt_version + offset,
        "request_sha256": request.sha256,
        "result_sha256": result_sha256,
        "schema_version": 1,
        "tombstone_sha256": None,
    }


def _nonfailed_receipt(
    request: protocol.Request,
    phase: str,
    offset: int,
) -> dict[str, protocol.JsonValue]:
    lock_inode = None
    result_sha256 = None
    if phase in {
        "acquired_held",
        "parent_running",
        "after_snapshot_written",
        "release_requested",
        "released",
    }:
        lock_inode = 123
    if phase in {"after_snapshot_written", "release_requested", "released"}:
        result_sha256 = "b" * 64
    return _receipt(
        request,
        phase=phase,
        offset=offset,
        lock_inode=lock_inode,
        result_sha256=result_sha256,
    )


@pytest.mark.parametrize(
    "container_id",
    ["short", "g" * 64, "f" * 63, "F" * 64],
)
def test_copied_receipt_rejects_non_full_docker_container_id(
    container_id: str,
) -> None:
    request = _request()
    payload = _nonfailed_receipt(request, "parent_running", 4)
    payload["container_id"] = container_id

    with pytest.raises(protocol.ProtocolError, match="container_id"):
        protocol.parse_receipt(payload, request)


@pytest.mark.parametrize(("phase", "offset"), tuple(_PHASE_OFFSETS.items()))
def test_copied_receipt_accepts_exact_nonfailed_phase_offset(
    phase: str,
    offset: int,
) -> None:
    request = _request()

    receipt = protocol.parse_receipt(
        _nonfailed_receipt(request, phase, offset),
        request,
    )

    assert receipt.phase == phase
    assert receipt.generation == request.generation + offset


@pytest.mark.parametrize(("phase", "offset"), tuple(_PHASE_OFFSETS.items()))
def test_copied_receipt_rejects_impossible_nonfailed_phase_offset(
    phase: str,
    offset: int,
) -> None:
    request = _request()

    with pytest.raises(protocol.ProtocolError, match=phase):
        protocol.parse_receipt(
            _nonfailed_receipt(request, phase, offset + 20),
            request,
        )


@pytest.mark.parametrize(
    ("offset", "lock_inode", "result_sha256"),
    [
        (2, None, None),
        (3, None, None),
        (4, 123, None),
        (5, 123, None),
        (6, 123, "b" * 64),
        (7, 123, "b" * 64),
    ],
)
def test_copied_failed_receipt_accepts_reachable_predecessor_offset(
    offset: int,
    lock_inode: int | None,
    result_sha256: str | None,
) -> None:
    request = _request()

    receipt = protocol.parse_receipt(
        _receipt(
            request,
            phase="failed",
            offset=offset,
            lock_inode=lock_inode,
            result_sha256=result_sha256,
            error="helper failed",
        ),
        request,
    )

    assert receipt.phase == "failed"


@pytest.mark.parametrize("offset", [1, 8, 100])
def test_copied_failed_receipt_rejects_unreachable_offset(offset: int) -> None:
    request = _request()

    with pytest.raises(protocol.ProtocolError, match="failed"):
        protocol.parse_receipt(
            _receipt(
                request,
                phase="failed",
                offset=offset,
                error="helper failed",
            ),
            request,
        )


@pytest.mark.parametrize(
    ("offset", "lock_inode", "result_sha256"),
    [
        (2, 123, None),
        (3, None, "b" * 64),
        (4, None, None),
        (5, 123, "b" * 64),
        (6, 123, None),
        (7, None, "b" * 64),
    ],
)
def test_copied_failed_receipt_rejects_predecessor_field_mismatch(
    offset: int,
    lock_inode: int | None,
    result_sha256: str | None,
) -> None:
    request = _request()

    with pytest.raises(protocol.ProtocolError, match="failed"):
        protocol.parse_receipt(
            _receipt(
                request,
                phase="failed",
                offset=offset,
                lock_inode=lock_inode,
                result_sha256=result_sha256,
                error="helper failed",
            ),
            request,
        )
