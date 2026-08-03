from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Final
from urllib import request

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_types import TransportName
from dokploy_wizard.litellm.model_admin import (
    PINNED_LITELLM_IMAGE,
    LiteLLMModelDeployment,
    build_owned_model_deployment,
)
from dokploy_wizard.litellm.model_admin_types import LiteLLMRoutingParams
from tests.integration.test_litellm_model_admin import (
    _docker,
    _pinned_litellm_service,
    _PinnedLiteLLMService,
)
from tests.unit._litellm_model_admin_support import catalog_model

pytestmark = pytest.mark.skipif(
    os.environ.get("DOKPLOY_WIZARD_REMOTE_LITELLM_ADMIN_TEST") != "1",
    reason="remote-only pinned LiteLLM wire integration",
)

_CAPTURE_SERVER: Final = r'''
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        headers = {name.lower(): value for name, value in self.headers.items()}
        Path("/tmp/request.json").write_text(json.dumps({
            "path": self.path,
            "authorization": headers.get("authorization"),
            "x_api_key": headers.get("x-api-key"),
            "anthropic_version": headers.get("anthropic-version"),
        }, sort_keys=True), encoding="utf-8")
        if self.path.endswith("/messages"):
            payload = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        else:
            payload = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''


@dataclass(frozen=True, slots=True)
class _CapturedRequest:
    path: str
    authorization: str | None
    x_api_key: str | None
    anthropic_version: str | None


@pytest.fixture(name="pinned_litellm_service")
def _wire_pinned_litellm_service() -> Iterator[_PinnedLiteLLMService]:
    yield from _pinned_litellm_service()


@pytest.fixture
def upstream_capture(
    pinned_litellm_service: _PinnedLiteLLMService,
) -> Iterator[tuple[_PinnedLiteLLMService, str]]:
    capture = f"{pinned_litellm_service.network}-capture"
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        capture,
        "--network",
        pinned_litellm_service.network,
        "--network-alias",
        "capture",
        "--entrypoint",
        "python3",
        PINNED_LITELLM_IMAGE,
        "-c",
        _CAPTURE_SERVER,
    )
    try:
        _wait_for_capture(capture)
        yield pinned_litellm_service, capture
    finally:
        _docker("rm", "--force", capture, check=False)


def test_pinned_openai_proxy_uses_exact_wire_contract(
    upstream_capture: tuple[_PinnedLiteLLMService, str],
) -> None:
    service, capture = upstream_capture
    deployment = _capture_deployment("minimax-m2.7", "openai")

    service.client.create_model(deployment)
    _proxy_chat(service, deployment.model_name)
    captured = _captured_request(capture)

    assert captured == _CapturedRequest(
        path="/zen/go/v1/chat/completions",
        authorization=f"Bearer {service.upstream_key}",
        x_api_key=None,
        anthropic_version=None,
    )


def test_pinned_anthropic_proxy_uses_exact_wire_contract(
    upstream_capture: tuple[_PinnedLiteLLMService, str],
) -> None:
    service, capture = upstream_capture
    deployment = _capture_deployment("deepseek-v4-flash", "anthropic")

    service.client.create_model(deployment)
    _proxy_chat(service, deployment.model_name)
    captured = _captured_request(capture)

    assert captured == _CapturedRequest(
        path="/zen/go/v1/messages",
        authorization=None,
        x_api_key=service.upstream_key,
        anthropic_version="2023-06-01",
    )


def _capture_deployment(
    source_id: str, transport: TransportName
) -> LiteLLMModelDeployment:
    model = catalog_model(source_id=source_id, transport=transport)
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    api_base = (
        "http://capture:8080/zen/go/v1"
        if transport == "openai"
        else "http://capture:8080/zen/go"
    )
    return replace(
        deployment,
        litellm_params=LiteLLMRoutingParams(
            model=f"{transport}/{source_id}",
            api_base=api_base,
            api_key="os.environ/LITELLM_OPENCODE_GO_API_KEY",
        ),
    )


def _proxy_chat(service: _PinnedLiteLLMService, model_name: str) -> None:
    raw_request = request.Request(
        f"{service.api_url}/chat/completions",
        method="POST",
        headers={
            "Authorization": f"Bearer {service.master_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {"model": model_name, "messages": [{"role": "user", "content": "test"}]}
        ).encode("utf-8"),
    )
    with request.urlopen(raw_request, timeout=30) as response:  # noqa: S310
        assert response.status == 200


def _wait_for_capture(container: str) -> None:
    deadline = time.monotonic() + 30
    probe = (
        "python3 -c \"import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2)\" "
        ">/dev/null 2>&1 && printf ready"
    )
    while time.monotonic() < deadline:
        completed = _docker(
            "exec",
            container,
            "sh",
            "-c",
            probe,
            check=False,
        )
        if completed == "ready":
            return
        time.sleep(1)
    pytest.fail("capture server did not become ready")


def _captured_request(container: str) -> _CapturedRequest:
    payload = _docker(
        "exec",
        container,
        "python3",
        "-c",
        "from pathlib import Path; print(Path('/tmp/request.json').read_text())",
    )
    parsed: JsonValue = json.loads(payload)
    assert isinstance(parsed, dict)
    path = parsed.get("path")
    authorization = parsed.get("authorization")
    x_api_key = parsed.get("x_api_key")
    anthropic_version = parsed.get("anthropic_version")
    assert isinstance(path, str)
    assert isinstance(authorization, str) or authorization is None
    assert isinstance(x_api_key, str) or x_api_key is None
    assert isinstance(anthropic_version, str) or anthropic_version is None
    return _CapturedRequest(path, authorization, x_api_key, anthropic_version)
