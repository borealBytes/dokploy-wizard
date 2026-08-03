"""Self-contained strict JSON contracts used by the copied lock helper."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from dokploy_wizard.dokploy import lock_helper_protocol_base as protocol_base
    from dokploy_wizard.dokploy import (
        lock_helper_protocol_validation as protocol_validation,
    )
elif __package__:
    from dokploy_wizard.dokploy import lock_helper_protocol_base as protocol_base
    from dokploy_wizard.dokploy import (
        lock_helper_protocol_validation as protocol_validation,
    )
else:
    import lock_helper_protocol_base as protocol_base
    import lock_helper_protocol_validation as protocol_validation

JsonValue: TypeAlias = protocol_base.JsonValue
Request: TypeAlias = protocol_base.Request
Receipt: TypeAlias = protocol_base.Receipt
Result: TypeAlias = protocol_base.Result
Release: TypeAlias = protocol_base.Release
ProtocolError = protocol_base.ProtocolError
canonical_sha256 = protocol_base.canonical_sha256
digest = protocol_base.digest
exact = protocol_base.exact
nonnegative_int = protocol_base.nonnegative_int
optional_digest = protocol_base.optional_digest
positive_int = protocol_base.positive_int
schema = protocol_base.schema
text = protocol_base.text
ContractValueError = protocol_validation.ContractValueError
optional_positive_int = protocol_validation.optional_positive_int
optional_text = protocol_validation.optional_text
optional_timestamp = protocol_validation.optional_timestamp
timestamp = protocol_validation.timestamp
validate_receipt_phase = protocol_validation.validate_receipt_phase

_REQUEST_KEYS = frozenset(
    {
        "config_sha256", "created_at", "env", "expected_state_sha256", "generation",
        "input_sha256", "lease", "mode", "parent_argv_sha256", "parent_pid",
        "parent_start_time_ticks", "receipt_version", "schema_version", "tombstone_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "container_id", "error", "generation", "heartbeat_at", "heartbeat_deadline_at",
        "lease", "lock_inode", "mode", "parent_argv_sha256", "parent_pid",
        "parent_start_time_ticks", "phase", "receipt_version", "request_sha256",
        "result_sha256", "schema_version", "tombstone_sha256",
    }
)
_RESULT_KEYS = frozenset(
    {
        "after_snapshot_sha256", "before_snapshot_sha256", "durable_write_delta",
        "ended_at", "generation", "lease", "parent_exit_code", "parent_reconcile_sha256",
        "request_sha256", "schema_version", "started_at", "status",
    }
)
_RELEASE_KEYS = frozenset(
    {
        "expected_receipt_version", "expected_result_sha256", "generation", "lease",
        "requested_at", "schema_version",
    }
)
_PHASES = frozenset(
    {
        "created", "starting", "acquired_held", "parent_running", "after_snapshot_written",
        "release_requested", "released", "failed",
    }
)


def parse_request(payload: dict[str, JsonValue]) -> Request:
    try:
        exact(payload, _REQUEST_KEYS, "request")
        schema(payload)
        lease = text(payload, "lease")
        generation = positive_int(payload, "generation")
        receipt_version = positive_int(payload, "receipt_version")
        if generation != receipt_version:
            raise ProtocolError("request generation/version relationship is invalid")
        mode = text(payload, "mode")
        if mode not in {"reconcile", "disable"}:
            raise ProtocolError("request mode is invalid")
        parent_pid = positive_int(payload, "parent_pid")
        start_ticks = nonnegative_int(payload, "parent_start_time_ticks")
        parent_argv = digest(payload, "parent_argv_sha256")
        for key in ("input_sha256", "config_sha256", "expected_state_sha256"):
            digest(payload, key)
        tombstone = payload["tombstone_sha256"]
        if tombstone is not None:
            digest(payload, "tombstone_sha256")
        timestamp(payload["created_at"], "created_at")
        env = payload["env"]
        if not isinstance(env, list):
            raise ProtocolError("request env is invalid")
        parsed_env: list[tuple[str, str]] = []
        for item in env:
            if not isinstance(item, dict) or set(item) != {"name", "value_sha256"}:
                raise ProtocolError("request env entry is invalid")
            parsed_env.append((text(item, "name"), digest(item, "value_sha256")))
        if parsed_env != sorted(set(parsed_env)):
            raise ProtocolError("request env is not sorted and unique")
    except ContractValueError as error:
        raise ProtocolError(str(error)) from error
    return Request(
        payload,
        lease,
        generation,
        receipt_version,
        mode,
        parent_pid,
        start_ticks,
        parent_argv,
        canonical_sha256(payload),
    )


def parse_receipt(payload: dict[str, JsonValue], request: Request) -> Receipt:
    try:
        exact(payload, _RECEIPT_KEYS, "receipt")
        schema(payload)
        lease = text(payload, "lease")
        generation = positive_int(payload, "generation")
        version = positive_int(payload, "receipt_version")
        phase = text(payload, "phase")
        container_id = digest(payload, "container_id")
        request_sha = digest(payload, "request_sha256")
        if phase not in _PHASES:
            raise ProtocolError("receipt phase is invalid")
        if (
            lease != request.lease
            or text(payload, "mode") != request.mode
            or positive_int(payload, "parent_pid") != request.parent_pid
            or nonnegative_int(payload, "parent_start_time_ticks")
            != request.parent_start_time_ticks
            or digest(payload, "parent_argv_sha256") != request.parent_argv_sha256
            or request_sha != request.sha256
        ):
            raise ProtocolError("receipt does not bind the request")
        result_sha = optional_digest(payload, "result_sha256")
        tombstone = optional_digest(payload, "tombstone_sha256")
        if tombstone != request.payload["tombstone_sha256"]:
            raise ProtocolError("receipt tombstone does not bind the request")
        heartbeat_at = optional_timestamp(payload["heartbeat_at"], "heartbeat_at")
        heartbeat_deadline = optional_timestamp(
            payload["heartbeat_deadline_at"],
            "heartbeat_deadline_at",
        )
        lock_inode = optional_positive_int(payload["lock_inode"], "lock_inode")
        error = optional_text(payload["error"], "error")
        validate_receipt_phase(
            phase=phase,
            generation_offset=generation - request.generation,
            version_offset=version - request.receipt_version,
            heartbeat_at=heartbeat_at,
            heartbeat_deadline_at=heartbeat_deadline,
            lock_inode=lock_inode,
            result_sha256=result_sha,
            error=error,
        )
    except ContractValueError as error:
        raise ProtocolError(str(error)) from error
    return Receipt(payload, lease, generation, version, phase, container_id, request_sha)


def parse_result(payload: dict[str, JsonValue], request: Request, receipt: Receipt) -> Result:
    exact(payload, _RESULT_KEYS, "result")
    schema(payload)
    if text(payload, "status") not in {"succeeded", "failed"}:
        raise ProtocolError("result status is invalid")
    for key in (
        "request_sha256", "before_snapshot_sha256", "after_snapshot_sha256",
        "parent_reconcile_sha256",
    ):
        digest(payload, key)
    delta = payload["durable_write_delta"]
    if not isinstance(delta, list) or not all(isinstance(item, str) for item in delta):
        raise ProtocolError("result durable delta is invalid")
    parsed_delta = [item for item in delta if isinstance(item, str)]
    if parsed_delta != sorted(set(parsed_delta)):
        raise ProtocolError("result durable delta is not sorted and unique")
    exit_code = payload["parent_exit_code"]
    if exit_code is not None and type(exit_code) is not int:
        raise ProtocolError("result exit code is invalid")
    started_at = _timestamp_value(payload, "started_at")
    ended_at = _timestamp_value(payload, "ended_at")
    if started_at < _timestamp_value(request.payload, "created_at") or ended_at < started_at:
        raise ProtocolError("result timestamps are invalid")
    result = Result(
        payload,
        text(payload, "lease"),
        positive_int(payload, "generation"),
        digest(payload, "request_sha256"),
        canonical_sha256(payload),
    )
    if (
        result.lease != request.lease
        or result.request_sha256 != request.sha256
        or result.generation != receipt.generation
    ):
        raise ProtocolError("result does not bind the request and receipt")
    status = text(payload, "status")
    exit_code = payload["parent_exit_code"]
    if (status == "succeeded" and exit_code != 0) or (
        status == "failed" and exit_code == 0
    ):
        raise ProtocolError("result status does not bind the parent exit code")
    return result


def parse_release(
    payload: dict[str, JsonValue],
    request: Request,
    receipt: Receipt,
    result: Result,
) -> Release:
    exact(payload, _RELEASE_KEYS, "release")
    schema(payload)
    requested_at = _timestamp_value(payload, "requested_at")
    release = Release(
        text(payload, "lease"),
        positive_int(payload, "generation"),
        positive_int(payload, "expected_receipt_version"),
        digest(payload, "expected_result_sha256"),
    )
    if (
        release.lease != request.lease
        or release.generation != receipt.generation
        or release.expected_receipt_version != receipt.receipt_version
        or release.expected_result_sha256 != result.sha256
    ):
        raise ProtocolError("release does not bind the request, receipt, and result")
    if requested_at < _timestamp_value(result.payload, "ended_at"):
        raise ProtocolError("release timestamp precedes the result")
    return release


def _timestamp_value(payload: dict[str, JsonValue], key: str) -> datetime:
    try:
        return timestamp(payload[key], key)
    except ContractValueError as error:
        raise ProtocolError(str(error)) from error
