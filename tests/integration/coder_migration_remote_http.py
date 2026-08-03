from __future__ import annotations

import http.client
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from tests.integration.coder_migration_remote_protocol import (
    CoderBuildInfo,
    FirstUserProbe,
    RemoteCoderProtocolError,
    RemoteCoderResponse,
    parse_build_info,
    parse_first_user_probe,
    parse_healthz,
)

_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS: Final = 3.0
_RETRY_DELAY_SECONDS: Final = 0.25
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class RemoteCoderEndpoint:
    port: int


@dataclass(frozen=True, slots=True)
class RemoteCoderRequest:
    method: str
    path: str
    payload: Mapping[str, JsonValue] | None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteCoderTransportError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ReadinessTiming:
    deadline_seconds: float
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]


@dataclass(frozen=True, slots=True)
class ReadinessStage(Generic[_Result]):
    path: str
    parse: Callable[[RemoteCoderResponse], _Result]


@dataclass(frozen=True, slots=True)
class ReadinessDeadline:
    expires_at: float
    timing: ReadinessTiming


def request_coder(
    endpoint: RemoteCoderEndpoint, request: RemoteCoderRequest
) -> RemoteCoderResponse:
    body = (
        None
        if request.payload is None
        else json.dumps(request.payload, separators=(",", ":")).encode("utf-8")
    )
    connection = http.client.HTTPConnection(
        "127.0.0.1", endpoint.port, timeout=_REQUEST_TIMEOUT_SECONDS
    )
    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    request_headers.update(request.headers)
    try:
        connection.request(request.method, request.path, body=body, headers=request_headers)
        response = connection.getresponse()
        return RemoteCoderResponse(
            status=response.status,
            body=_read_response(response),
            headers=tuple((name.lower(), value) for name, value in response.getheaders()),
        )
    except (http.client.HTTPException, OSError) as error:
        raise RemoteCoderTransportError("remote Coder request transport failed") from error
    finally:
        connection.close()


def wait_for_coder_readiness(
    read: Callable[[str], RemoteCoderResponse],
    timing: ReadinessTiming,
) -> FirstUserProbe:
    deadline = ReadinessDeadline(timing.monotonic() + timing.deadline_seconds, timing)
    _retry_read(read, ReadinessStage("/healthz", parse_healthz), deadline)
    _retry_read(read, ReadinessStage("/api/v2/buildinfo", parse_build_info), deadline)
    return _retry_read(
        read, ReadinessStage("/api/v2/users/first", parse_first_user_probe), deadline
    )


def _retry_read(
    read: Callable[[str], RemoteCoderResponse],
    stage: ReadinessStage[_Result],
    deadline: ReadinessDeadline,
) -> _Result:
    timing = deadline.timing
    while timing.monotonic() < deadline.expires_at:
        try:
            response = read(stage.path)
        except RemoteCoderTransportError:
            timing.sleep(_RETRY_DELAY_SECONDS)
            continue
        if 500 <= response.status < 600:
            timing.sleep(_RETRY_DELAY_SECONDS)
            continue
        return stage.parse(response)
    raise RemoteCoderProtocolError(f"Coder readiness deadline expired at {stage.path}")


def _read_response(response: http.client.HTTPResponse) -> bytes:
    payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise RemoteCoderTransportError("remote Coder response exceeds the bounded transport limit")
    return payload


__all__ = [
    "CoderBuildInfo",
    "ReadinessTiming",
    "RemoteCoderRequest",
    "RemoteCoderEndpoint",
    "RemoteCoderTransportError",
    "request_coder",
    "wait_for_coder_readiness",
]
