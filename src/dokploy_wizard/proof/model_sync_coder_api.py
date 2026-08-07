from __future__ import annotations

import json
from http.client import HTTPMessage
from ipaddress import ip_address
from typing import IO, Final, Literal
from urllib import request

from dokploy_wizard.dokploy.coder import _coder_container_name
from dokploy_wizard.proof.model_sync_artifacts import (
    JsonValue,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_task1_context import active_task1_proof_context

_OUTPUT_LIMIT: Final = 2 * 1024 * 1024


CoderSnapshotStage = Literal["route", "endpoint", "payload"]


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
    except (CoderSnapshotApiError, ValueError, OSError):
        raise CoderSnapshotApiError("route") from None
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
    try:
        container = _coder_container_name(f"{stack_name}-coder")
    except ValueError:
        raise CoderSnapshotApiError("route") from None
    if container is None:
        raise CoderSnapshotApiError("route")
    raw = run_bounded_process(
        ["docker", "inspect", "--type", "container", container],
        stdin=b"",
        output_limit=_OUTPUT_LIMIT,
        timeout_seconds=30,
        label="Coder internal API container inspect",
    )
    values = require_list(json.loads(raw), "Coder internal API container inspect")
    if len(values) != 1:
        raise CoderSnapshotApiError("route")
    inspected = require_mapping(values[0], "Coder internal API container inspect")
    settings = require_mapping(inspected.get("NetworkSettings"), "Coder network settings")
    networks = require_mapping(settings.get("Networks"), "Coder networks")
    shared_value = networks.get(f"{stack_name}-shared")
    if shared_value is None:
        raise CoderSnapshotApiError("route")
    shared = require_mapping(shared_value, "Coder shared network")
    address = require_text(shared.get("IPAddress"), "Coder shared network address")
    try:
        parsed = ip_address(address)
    except ValueError as error:
        raise CoderSnapshotApiError("route") from error
    if not parsed.is_private:
        raise CoderSnapshotApiError("route")
    authority = f"[{address}]" if parsed.version == 6 else address
    return f"http://{authority}:3000"


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
