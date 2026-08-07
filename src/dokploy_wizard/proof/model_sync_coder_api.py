from __future__ import annotations

import json
from enum import StrEnum
from http.client import HTTPMessage
from ipaddress import ip_address
from typing import IO, Final, Literal
from urllib import request

from dokploy_wizard.dokploy.coder import _coder_container_name
from dokploy_wizard.packs.coder.reconciler import CoderError
from dokploy_wizard.proof.model_sync_artifacts import (
    JsonValue,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_task1_context import active_task1_proof_context

_OUTPUT_LIMIT: Final = 2 * 1024 * 1024


CoderContainerStage = Literal[
    "container_missing",
    "container_not_running",
    "container_restarting",
    "container_ambiguous",
    "container_discovery_unavailable",
    "container_discovery_inconsistent",
]
CoderSnapshotStage = CoderContainerStage | Literal[
    "inspect", "network", "address", "endpoint", "payload"
]


class DockerContainerState(StrEnum):
    """Docker state values accepted from the all-container projection."""

    CREATED = "created"
    RESTARTING = "restarting"
    RUNNING = "running"
    REMOVING = "removing"
    PAUSED = "paused"
    EXITED = "exited"
    DEAD = "dead"


class CoderContainerObservationError(ValueError):
    """Raised when the all-container state projection is not allowlisted."""


class CoderSnapshotApiError(ValueError):
    """Raised when the bounded Coder snapshot API contract is unavailable."""

    def __init__(self, stage: CoderSnapshotStage) -> None:
        super().__init__("Coder snapshot API is unavailable")
        self.stage = stage


def coder_login(hostname: str, email: str, password: str) -> str:
    response = api(
        hostname,
        None,
        "/api/v2/users/login",
        {"email": email, "password": password},
    )
    return _field(response, "session_token")


def api(
    hostname: str,
    token: str | None,
    path: str,
    body: dict[str, str] | None = None,
) -> dict[str, JsonValue] | list[JsonValue]:
    value = _api_value(hostname, token, path, body)
    if isinstance(value, (dict, list)):
        return value
    raise CoderSnapshotApiError("payload")


def nullable_api(
    hostname: str, token: str | None, path: str
) -> JsonValue:
    """Read a Coder endpoint whose documented empty response is JSON null."""
    value = _api_value(hostname, token, path, None)
    if value is None or isinstance(value, (dict, list)):
        return value
    raise CoderSnapshotApiError("payload")


def _api_value(
    hostname: str, token: str | None, path: str, body: dict[str, str] | None
) -> JsonValue:
    headers = {
        "Accept": "application/json",
        "Host": hostname,
        **({"Coder-Session-Token": token} if token is not None else {}),
    }
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        url = _api_url(hostname, path)
    except CoderSnapshotApiError:
        raise
    except (ValueError, OSError):
        raise CoderSnapshotApiError("endpoint") from None
    request_value = request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if body else "GET",
    )
    opener = request.build_opener(_NoRedirect())
    try:
        with opener.open(request_value, timeout=30) as response:  # noqa: S310
            encoded = response.read(_OUTPUT_LIMIT + 1)
    except OSError:
        raise CoderSnapshotApiError("endpoint") from None
    if len(encoded) > _OUTPUT_LIMIT:
        raise CoderSnapshotApiError("payload")
    try:
        raw: JsonValue = json.loads(encoded.decode("utf-8"))
        return raw
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CoderSnapshotApiError("payload") from None


def _api_url(hostname: str, path: str) -> str:
    context = active_task1_proof_context()
    if context is None:
        return f"https://{hostname}{path}"
    return f"{_internal_base_url(context.stack_name)}{path}"


def _internal_base_url(stack_name: str) -> str:
    service_name = f"{stack_name}-coder"
    try:
        container = _coder_container_name(service_name)
    except CoderError:
        raise CoderSnapshotApiError(_unresolved_container_stage(service_name)) from None
    if container is None:
        raise CoderSnapshotApiError(_unresolved_container_stage(service_name))
    try:
        raw = run_bounded_process(
            ["docker", "inspect", "--type", "container", container],
            stdin=b"",
            output_limit=_OUTPUT_LIMIT,
            timeout_seconds=30,
            label="Coder internal API container inspect",
        )
        values = require_list(json.loads(raw), "Coder internal API container inspect")
    except (RuntimeError, ValueError, json.JSONDecodeError):
        raise CoderSnapshotApiError("inspect") from None
    if len(values) != 1:
        raise CoderSnapshotApiError("inspect")
    try:
        inspected = require_mapping(values[0], "Coder internal API container inspect")
        settings = require_mapping(inspected.get("NetworkSettings"), "Coder network settings")
        networks = require_mapping(settings.get("Networks"), "Coder networks")
    except RuntimeError:
        raise CoderSnapshotApiError("inspect") from None
    shared_value = networks.get(f"{stack_name}-shared")
    if shared_value is None:
        raise CoderSnapshotApiError("network")
    try:
        shared = require_mapping(shared_value, "Coder shared network")
        address = require_text(shared.get("IPAddress"), "Coder shared network address")
    except RuntimeError:
        raise CoderSnapshotApiError("network") from None
    try:
        parsed = ip_address(address)
    except ValueError as error:
        raise CoderSnapshotApiError("address") from error
    if not parsed.is_private:
        raise CoderSnapshotApiError("address")
    authority = f"[{address}]" if parsed.version == 6 else address
    return f"http://{authority}:3000"


def _unresolved_container_stage(service_name: str) -> CoderContainerStage:
    try:
        raw = run_bounded_process(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.service={service_name}",
                "--format",
                "{{.State}}",
            ],
            stdin=b"",
            output_limit=_OUTPUT_LIMIT,
            timeout_seconds=30,
            label="Coder all-container state",
        )
        states = _all_container_states(raw)
    except (CoderContainerObservationError, RuntimeError, UnicodeDecodeError):
        return "container_discovery_unavailable"
    if DockerContainerState.RUNNING in states:
        return "container_discovery_inconsistent"
    if len(states) == 0:
        return "container_missing"
    if len(states) > 1:
        return "container_ambiguous"
    if states == (DockerContainerState.RESTARTING,):
        return "container_restarting"
    return "container_not_running"


def _all_container_states(raw: bytes) -> tuple[DockerContainerState, ...]:
    return tuple(_container_state(value) for value in raw.decode("ascii").splitlines())


def _container_state(value: str) -> DockerContainerState:
    try:
        return DockerContainerState(value)
    except ValueError:
        raise CoderContainerObservationError("Coder all-container state is invalid") from None


def _field(value: JsonValue, key: str) -> str:
    return require_text(require_mapping(value, "Coder response").get(key), f"Coder response {key}")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: request.Request,
        _fp: IO[bytes],
        _code: int,
        _msg: str,
        _headers: HTTPMessage,
        _new_url: str,
    ) -> None:
        return None
