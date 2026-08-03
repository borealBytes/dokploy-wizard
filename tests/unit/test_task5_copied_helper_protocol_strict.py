from __future__ import annotations

from copy import deepcopy

import pytest

from dokploy_wizard.dokploy import lock_helper_protocol as protocol

_LEASE = "97099d6d-fd71-4ae0-8779-df6de483dfcc"
_DIGEST = "a" * 64


def _request_payload() -> dict[str, protocol.JsonValue]:
    return {
        "config_sha256": "b" * 64,
        "created_at": "2026-07-29T00:00:00+00:00",
        "env": [{"name": "TZ", "value_sha256": "c" * 64}],
        "expected_state_sha256": "d" * 64,
        "generation": 1,
        "input_sha256": "e" * 64,
        "lease": _LEASE,
        "mode": "reconcile",
        "parent_argv_sha256": _DIGEST,
        "parent_pid": 12,
        "parent_start_time_ticks": 34,
        "receipt_version": 1,
        "schema_version": 1,
        "tombstone_sha256": None,
    }


def _receipt_payload(request_sha256: str) -> dict[str, protocol.JsonValue]:
    return {
        "container_id": "f" * 64,
        "error": None,
        "generation": 5,
        "heartbeat_at": "2026-07-29T00:00:01+00:00",
        "heartbeat_deadline_at": "2026-07-29T00:00:06+00:00",
        "lease": _LEASE,
        "lock_inode": 123,
        "mode": "reconcile",
        "parent_argv_sha256": _DIGEST,
        "parent_pid": 12,
        "parent_start_time_ticks": 34,
        "phase": "parent_running",
        "receipt_version": 5,
        "request_sha256": request_sha256,
        "result_sha256": None,
        "schema_version": 1,
        "tombstone_sha256": None,
    }


def _result_payload(request_sha256: str) -> dict[str, protocol.JsonValue]:
    return {
        "after_snapshot_sha256": "1" * 64,
        "before_snapshot_sha256": "2" * 64,
        "durable_write_delta": ["applied-state.json"],
        "ended_at": "2026-07-29T00:00:03+00:00",
        "generation": 5,
        "lease": _LEASE,
        "parent_exit_code": 0,
        "parent_reconcile_sha256": "3" * 64,
        "request_sha256": request_sha256,
        "schema_version": 1,
        "started_at": "2026-07-29T00:00:02+00:00",
        "status": "succeeded",
    }


def _release_payload(result_sha256: str) -> dict[str, protocol.JsonValue]:
    return {
        "expected_receipt_version": 5,
        "expected_result_sha256": result_sha256,
        "generation": 5,
        "lease": _LEASE,
        "requested_at": "2026-07-29T00:00:04+00:00",
        "schema_version": 1,
    }


def test_copied_protocol_parses_exact_cross_document_bindings() -> None:
    request = protocol.parse_request(_request_payload())
    receipt = protocol.parse_receipt(_receipt_payload(request.sha256), request)
    result = protocol.parse_result(_result_payload(request.sha256), request, receipt)
    release = protocol.parse_release(
        _release_payload(result.sha256),
        request,
        receipt,
        result,
    )

    assert result.generation == receipt.generation
    assert release.expected_receipt_version == receipt.receipt_version


def test_copied_request_rejects_noncanonical_timestamp() -> None:
    payload = _request_payload()
    payload["created_at"] = "yesterday"

    with pytest.raises(protocol.ProtocolError, match="created_at"):
        protocol.parse_request(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("heartbeat_at", 1),
        ("heartbeat_deadline_at", "not-a-time"),
        ("lock_inode", "123"),
        ("error", 7),
    ],
)
def test_copied_receipt_rejects_unparsed_optional_values(
    field: str,
    value: protocol.JsonValue,
) -> None:
    request = protocol.parse_request(_request_payload())
    payload = _receipt_payload(request.sha256)
    payload[field] = value

    with pytest.raises(protocol.ProtocolError, match=field):
        protocol.parse_receipt(payload, request)


def test_copied_receipt_rejects_phase_and_tombstone_invariants() -> None:
    request = protocol.parse_request(_request_payload())
    created = _receipt_payload(request.sha256)
    created.update(
        {
            "generation": 1,
            "phase": "created",
            "receipt_version": 1,
        }
    )

    with pytest.raises(protocol.ProtocolError, match="created"):
        protocol.parse_receipt(created, request)

    mismatched = _receipt_payload(request.sha256)
    mismatched["tombstone_sha256"] = "9" * 64
    with pytest.raises(protocol.ProtocolError, match="tombstone"):
        protocol.parse_receipt(mismatched, request)


def test_copied_result_and_release_reject_cross_generation_bindings() -> None:
    request = protocol.parse_request(_request_payload())
    receipt = protocol.parse_receipt(_receipt_payload(request.sha256), request)
    bad_result = deepcopy(_result_payload(request.sha256))
    bad_result["generation"] = receipt.generation + 1

    with pytest.raises(protocol.ProtocolError, match="result.*bind"):
        protocol.parse_result(bad_result, request, receipt)

    result = protocol.parse_result(_result_payload(request.sha256), request, receipt)
    bad_release = _release_payload(result.sha256)
    bad_release["expected_receipt_version"] = receipt.receipt_version + 1
    with pytest.raises(protocol.ProtocolError, match="release.*bind"):
        protocol.parse_release(bad_release, request, receipt, result)
