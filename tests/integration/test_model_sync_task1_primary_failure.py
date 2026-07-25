from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dokploy_wizard.proof import model_sync_task1_baseline_runner as baseline_runner
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
