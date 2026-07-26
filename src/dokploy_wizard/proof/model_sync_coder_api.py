from __future__ import annotations

import json
from http.client import HTTPMessage
from ipaddress import ip_address
from typing import IO, Final
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


class CoderSnapshotApiError(ValueError):
    """Raised when the bounded Coder snapshot API contract is unavailable."""


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
    headers = {
        "Accept": "application/json",
        "Host": hostname,
        **({"Coder-Session-Token": token} if token is not None else {}),
    }
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request_value = request.Request(
        _api_url(hostname, path),
        data=data,
        headers=headers,
        method="POST" if body else "GET",
    )
    opener = request.build_opener(_NoRedirect())
    with opener.open(request_value, timeout=30) as response:  # noqa: S310
        encoded = response.read(_OUTPUT_LIMIT + 1)
    if len(encoded) > _OUTPUT_LIMIT:
        raise CoderSnapshotApiError("Coder API response exceeds the capture limit")
    raw = json.loads(encoded.decode("utf-8"))
    if not isinstance(raw, (dict, list)):
        raise CoderSnapshotApiError("Coder API returned an unsupported JSON shape")
    return raw


def _api_url(hostname: str, path: str) -> str:
    context = active_task1_proof_context()
    if context is None:
        return f"https://{hostname}{path}"
    return f"{_internal_base_url(context.stack_name)}{path}"


def _internal_base_url(stack_name: str) -> str:
    container = _coder_container_name(f"{stack_name}-coder")
    if container is None:
        raise CoderSnapshotApiError("Coder container is not running")
    raw = run_bounded_process(
        ["docker", "inspect", "--type", "container", container],
        stdin=b"",
        output_limit=_OUTPUT_LIMIT,
        timeout_seconds=30,
        label="Coder internal API container inspect",
    )
    values = require_list(json.loads(raw), "Coder internal API container inspect")
    if len(values) != 1:
        raise CoderSnapshotApiError("Coder internal API container inspect must return one object")
    inspected = require_mapping(values[0], "Coder internal API container inspect")
    settings = require_mapping(inspected.get("NetworkSettings"), "Coder network settings")
    networks = require_mapping(settings.get("Networks"), "Coder networks")
    shared_value = networks.get(f"{stack_name}-shared")
    if shared_value is None:
        raise CoderSnapshotApiError("Coder shared network is absent")
    shared = require_mapping(shared_value, "Coder shared network")
    address = require_text(shared.get("IPAddress"), "Coder shared network address")
    try:
        parsed = ip_address(address)
    except ValueError as error:
        raise CoderSnapshotApiError("Coder shared network address is invalid") from error
    if not parsed.is_private:
        raise CoderSnapshotApiError("Coder shared network address must be private")
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
