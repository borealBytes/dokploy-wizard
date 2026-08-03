from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Final, Literal
from urllib import error, request
from uuid import uuid4

import pytest

from dokploy_wizard.litellm.model_admin import (
    PINNED_LITELLM_IMAGE,
    LiteLLMModelAdminClient,
    build_owned_model_deployment,
)
from tests.unit._litellm_model_admin_support import catalog_model

pytestmark = pytest.mark.skipif(
    os.environ.get("DOKPLOY_WIZARD_REMOTE_LITELLM_ADMIN_TEST") != "1",
    reason="remote-only pinned LiteLLM integration",
)

_POSTGRES_IMAGE = (
    "cimg/postgres:16.0@sha256:b125148bc76e8e8eee5eb3ad6020a3a14110a14e8192f1c645128afebe2e2f84"
)
READINESS_TIMEOUT_SECONDS: Final = 300


class PinnedLiteLLMServiceError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _PinnedLiteLLMService:
    client: LiteLLMModelAdminClient
    api_url: str
    litellm_container: str
    network: str
    master_key: str
    salt_key: str
    upstream_key: str


@pytest.fixture
def pinned_litellm_service() -> Iterator[_PinnedLiteLLMService]:
    yield from _pinned_litellm_service()


def _pinned_litellm_service() -> Iterator[_PinnedLiteLLMService]:
    if os.environ.get("LITELLM_TEST_IMAGE") != PINNED_LITELLM_IMAGE:
        raise PinnedLiteLLMServiceError("LITELLM_TEST_IMAGE must be the Task 1 accepted digest")
    if os.environ.get("POSTGRES_TEST_IMAGE") != _POSTGRES_IMAGE:
        raise PinnedLiteLLMServiceError("POSTGRES_TEST_IMAGE must be the Task 3 pinned digest")

    run_id = os.environ.get("DOKPLOY_WIZARD_TASK3_RUN_ID")
    if run_id is None:
        raise PinnedLiteLLMServiceError(
            "DOKPLOY_WIZARD_TASK3_RUN_ID is required for remote cleanup"
        )
    network = f"dw-litellm-model-admin-{run_id}"
    postgres = f"{network}-postgres"
    litellm = f"{network}-litellm"
    master_key = f"sk-{uuid4().hex}"
    salt_key = uuid4().hex
    upstream_key = f"sk-{uuid4().hex}"
    _docker("network", "create", network)
    try:
        _docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            postgres,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--env",
            "POSTGRES_DB=litellm",
            "--env",
            "POSTGRES_USER=litellm",
            "--env",
            "POSTGRES_PASSWORD=litellm",
            _POSTGRES_IMAGE,
        )
        _docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            litellm,
            "--network",
            network,
            "--publish",
            "127.0.0.1::4000",
            "--env",
            "DATABASE_URL=postgresql://litellm:litellm@postgres:5432/litellm",
            "--env",
            f"LITELLM_MASTER_KEY={master_key}",
            "--env",
            f"LITELLM_SALT_KEY={salt_key}",
            "--env",
            "STORE_MODEL_IN_DB=True",
            "--env",
            f"LITELLM_OPENCODE_GO_API_KEY={upstream_key}",
            PINNED_LITELLM_IMAGE,
            "--port",
            "4000",
        )
        port = _docker("port", litellm, "4000/tcp").rsplit(":", maxsplit=1)[1]
        service = _PinnedLiteLLMService(
            client=LiteLLMModelAdminClient(
                api_url=f"http://127.0.0.1:{port}",
                master_key=master_key,
            ),
            api_url=f"http://127.0.0.1:{port}",
            litellm_container=litellm,
            network=network,
            master_key=master_key,
            salt_key=salt_key,
            upstream_key=upstream_key,
        )
        _wait_for_readiness(service.api_url)
        yield service
    finally:
        _docker("rm", "--force", litellm, check=False)
        _docker("rm", "--force", postgres, check=False)
        _docker("network", "rm", network, check=False)


def test_bootstrap_static_db_coexistence_after_restart(
    pinned_litellm_service: _PinnedLiteLLMService,
) -> None:
    static_openai = build_owned_model_deployment(catalog_model(), bootstrap_static=True)
    static_anthropic = build_owned_model_deployment(
        catalog_model(source_id="deepseek-v4-flash", transport="anthropic"),
        bootstrap_static=True,
    )

    pinned_litellm_service.client.create_model(static_openai)
    pinned_litellm_service.client.create_model(static_anthropic)
    restarted_service = _recreate_litellm_service(pinned_litellm_service)
    _wait_for_readiness(restarted_service.api_url)
    records = restarted_service.client.list_models()

    assert {record.model_id for record in records} == {
        static_openai.model_id,
        static_anthropic.model_id,
    }


def test_cached_usage_cost_projection_persists_after_cutover(
    pinned_litellm_service: _PinnedLiteLLMService,
) -> None:
    static_deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=True)
    cutover_deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    pinned_litellm_service.client.create_model(static_deployment)
    updated = pinned_litellm_service.client.update_model(cutover_deployment)
    pinned_litellm_service.client.delete_model(cutover_deployment.model_id)

    assert updated.model_info["bootstrap_static"] is False
    assert updated.model_info["cache_read_input_token_cost"] == 0.00000006
    assert updated.model_info["cache_creation_input_token_cost"] == 0.000000375
    assert pinned_litellm_service.client.list_models() == ()


def _docker(*args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ("docker", *args),
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def _recreate_litellm_service(service: _PinnedLiteLLMService) -> _PinnedLiteLLMService:
    _docker("rm", "--force", service.litellm_container)
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        service.litellm_container,
        "--network",
        service.network,
        "--publish",
        "127.0.0.1::4000",
        "--env",
        "DATABASE_URL=postgresql://litellm:litellm@postgres:5432/litellm",
        "--env",
        f"LITELLM_MASTER_KEY={service.master_key}",
        "--env",
        f"LITELLM_SALT_KEY={service.salt_key}",
        "--env",
        "STORE_MODEL_IN_DB=True",
        "--env",
        f"LITELLM_OPENCODE_GO_API_KEY={service.upstream_key}",
        PINNED_LITELLM_IMAGE,
        "--port",
        "4000",
    )
    port = _docker("port", service.litellm_container, "4000/tcp").rsplit(":", maxsplit=1)[1]
    api_url = f"http://127.0.0.1:{port}"
    return replace(
        service,
        client=LiteLLMModelAdminClient(api_url=api_url, master_key=service.master_key),
        api_url=api_url,
    )


def _wait_for_readiness(api_url: str) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    last_status: int | Literal["connection-refused", "transport"] = "transport"
    while time.monotonic() < deadline:
        last_status = _readiness_status(api_url)
        if last_status == 200:
            return
        time.sleep(1)
    raise PinnedLiteLLMServiceError(
        f"pinned LiteLLM service did not become ready (last status: {last_status})"
    )


def _readiness_status(api_url: str) -> int | Literal["connection-refused", "transport"]:
    try:
        with request.urlopen(f"{api_url}/health/readiness", timeout=2) as response:  # noqa: S310
            return int(response.status)
    except error.HTTPError as exc:
        return exc.code
    except ConnectionResetError:
        return "transport"
    except error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return "connection-refused"
        return "transport"
