from __future__ import annotations

from email.message import Message
from typing import NoReturn
from urllib import error

import pytest

from dokploy_wizard.litellm.model_admin import PINNED_LITELLM_IMAGE, LiteLLMModelAdminClient
from tests.integration import test_litellm_model_admin as remote_model_admin
from tests.integration._litellm_model_admin_remote_runner import (
    POSTGRES_TEST_IMAGE,
    PYTEST_RUNNER_PACKAGES,
    classify_pytest_log,
    classify_runner_marker,
    pytest_runner_dependency_paths,
    remote_runner_command,
)
from tests.integration.test_litellm_model_admin import READINESS_TIMEOUT_SECONDS


def test_pinned_remote_runner_uses_vendored_pytest_and_requires_a_zero_skip_summary() -> None:
    assert "typing_extensions" in PYTEST_RUNNER_PACKAGES
    assert "py" in PYTEST_RUNNER_PACKAGES
    dependency_paths = pytest_runner_dependency_paths()
    assert any(path.name == "typing_extensions.py" for path in dependency_paths)
    assert all(path.name != "site-packages" for path in dependency_paths)
    command = remote_runner_command(
        remote_root="/tmp/dw-task3-runner-test",
        selector="tests/integration/test_litellm_model_admin.py -k owned_delete",
        run_id="runner-test",
    )

    assert command[:7] == (
        "docker",
        "run",
        "--detach",
        "--name",
        "dw-task3-runner-runner-test",
        "--network",
        "host",
    )
    assert "--rm" not in command
    assert "host" in command
    assert f"LITELLM_TEST_IMAGE={PINNED_LITELLM_IMAGE}" in command
    assert f"POSTGRES_TEST_IMAGE={POSTGRES_TEST_IMAGE}" in command
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "DOKPLOY_WIZARD_TASK3_RUN_ID=runner-test" in command
    entrypoint_index = command.index("--entrypoint")
    assert command[entrypoint_index + 1] == "sh"
    assert command[-3] == PINNED_LITELLM_IMAGE
    assert "pytest -q tests/integration/test_litellm_model_admin.py -k owned_delete" in command[-1]
    assert "grep -Eo '^[0-9]+ passed(, [0-9]+ deselected)?'" in command[-1]
    assert "tr -s ' ,' '-'" in command[-1]
    assert classify_runner_marker("TASK3_STATUS=0 TASK3_SUMMARY=5-passed-2-deselected") == (
        "5-passed-2-deselected"
    )
    assert classify_runner_marker("TASK3_STATUS=0 TASK3_SUMMARY=5-passed-1-skipped") is None
    assert classify_runner_marker("TASK3_STATUS=1 TASK3_SUMMARY=no-summary") is None
    assert (
        classify_pytest_log("LiteLLM model routing parameters must contain exactly three keys")
        == "routing-contract"
    )
    assert classify_pytest_log("No module named 'pluggy'") == "pytest-dependency"


def test_pinned_litellm_readiness_budget_covers_cold_database_migration() -> None:
    assert READINESS_TIMEOUT_SECONDS >= 300


def test_readiness_status_preserves_the_last_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_: str, *, timeout: int) -> NoReturn:
        del timeout
        raise error.HTTPError(
            "http://litellm.internal/health/readiness", 503, "", Message(), None
        )

    monkeypatch.setattr("tests.integration.test_litellm_model_admin.request.urlopen", unavailable)

    assert remote_model_admin._readiness_status("http://litellm.internal") == 503


def test_readiness_status_distinguishes_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_: str, *, timeout: int) -> NoReturn:
        del timeout
        raise error.URLError(ConnectionRefusedError())

    monkeypatch.setattr("tests.integration.test_litellm_model_admin.request.urlopen", refused)

    assert remote_model_admin._readiness_status("http://litellm.internal") == "connection-refused"


def test_pinned_service_restart_recreates_only_the_litellm_writable_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def docker(*args: str, check: bool = True) -> str:
        del check
        calls.append(args)
        if args[0] == "port":
            return "127.0.0.1:43000"
        return ""

    monkeypatch.setattr(remote_model_admin, "_docker", docker)
    service = remote_model_admin._PinnedLiteLLMService(
        client=LiteLLMModelAdminClient(
            api_url="http://127.0.0.1:42000",
            master_key="synthetic-master",
        ),
        api_url="http://127.0.0.1:42000",
        litellm_container="litellm-test",
        network="litellm-network-test",
        master_key="synthetic-master",
        salt_key="synthetic-salt",
        upstream_key="synthetic-upstream",
    )

    restarted = remote_model_admin._recreate_litellm_service(service)

    assert calls[0] == ("rm", "--force", "litellm-test")
    assert calls[1][:7] == (
        "run",
        "--detach",
        "--rm",
        "--name",
        "litellm-test",
        "--network",
        "litellm-network-test",
    )
    assert calls[2] == ("port", "litellm-test", "4000/tcp")
    assert "LITELLM_OPENCODE_GO_API_KEY=synthetic-upstream" in calls[1]
    assert restarted.api_url == "http://127.0.0.1:43000"
    assert restarted.litellm_container == service.litellm_container
    assert restarted.network == service.network
