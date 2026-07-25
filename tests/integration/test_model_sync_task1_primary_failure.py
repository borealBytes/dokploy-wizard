from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dokploy_wizard.cli import _task1_install_error_message
from dokploy_wizard.dokploy.cloudflared import (
    CloudflaredConnectorError,
    CloudflaredFailureCategory,
)
from dokploy_wizard.proof import model_sync_task1_baseline_runner as baseline_runner
from dokploy_wizard.proof.model_sync_cli_wrapper import _classify_task1_nonzero
from dokploy_wizard.proof.model_sync_task1_remote_abort import clear_remote_abort_record
from tests.integration._model_sync_task1_finalization_recovery_support import (
    runner_args,
    task1_recovery_fixture,
)
from tests.integration.test_model_sync_task1_finalization_recovery_abort import (
    ProofWrapperCall,
    _configure_pre_plan_failure,
)


class CleanupFailure(RuntimeError):
    """Raised when the fake remote cleanup fails after the primary error."""


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            b"Dokploy API request failed with status 400: invalid field: composeFile SECRET",
            "Dokploy API status 400 rejected field composeFile",
        ),
        (
            b"Dokploy compose.deploy response must include boolean success. SECRET",
            "Dokploy compose.deploy response must include boolean success.",
        ),
        (
            b"Dokploy public URL did not become reachable: https://SECRET.example.test",
            "Cloudflare connector health check failed",
        ),
        (
            b"Dokploy compose.update serviceName must be a string or null. SECRET",
            "Dokploy compose.update serviceName must be a string or null.",
        ),
        (
            b"Dokploy API request failed: SECRET.example.test unavailable.",
            "Dokploy API transport failed",
        ),
    ],
)
def test_task1_nonzero_classification_retains_only_value_free_cause(
    stderr: bytes, expected: str
) -> None:
    error = _classify_task1_nonzero(stderr)

    assert str(error) == expected
    assert "SECRET" not in str(error)


def test_task1_connector_category_crosses_cli_and_wrapper_without_message() -> None:
    source = CloudflaredConnectorError(
        "SECRET https://host.example.test",
        category=CloudflaredFailureCategory.DEPLOY_COMPOSE,
    )
    marker = _task1_install_error_message(source, task1_context_enabled=True)

    error = _classify_task1_nonzero(f"prefix {marker} suffix SECRET".encode())

    assert str(error) == "Task 1 remote failure category: cloudflared.deploy_compose"
    assert "SECRET" not in str(error)
    assert "host.example.test" not in str(error)


def test_wrapper_failure_is_not_masked_when_remote_cleanup_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    clear_remote_abort_record(fixture.paths.guard_path, fixture.host)
    cleanup_calls: list[str] = []
    wrapper_calls: list[ProofWrapperCall] = []
    _configure_pre_plan_failure(
        monkeypatch,
        fixture,
        "wrapper",
        "restored",
        cleanup_calls,
        wrapper_calls,
    )

    with pytest.raises(RuntimeError, match="wrapper failed"):
        baseline_runner.run_baseline_host_a(runner_args(fixture.paths))

    assert cleanup_calls == ["wrapper"]


def test_wrapper_failure_is_not_masked_when_remote_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    clear_remote_abort_record(fixture.paths.guard_path, fixture.host)
    cleanup_calls: list[str] = []
    wrapper_calls: list[ProofWrapperCall] = []
    _configure_pre_plan_failure(
        monkeypatch,
        fixture,
        "wrapper",
        "restored",
        cleanup_calls,
        wrapper_calls,
    )

    def failed_cleanup(
        _args: Namespace,
        _host: str,
        _password: str,
        _preparation: SimpleNamespace,
    ) -> bytes:
        raise CleanupFailure("cleanup failed")

    monkeypatch.setattr(baseline_runner, "_cleanup_report", failed_cleanup)

    with pytest.raises(RuntimeError, match="wrapper failed"):
        baseline_runner.run_baseline_host_a(runner_args(fixture.paths))
