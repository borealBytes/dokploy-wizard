from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.sync_immediate_executor import (
    DockerExecImmediateSyncExecutor,
    parse_immediate_sync_command,
)
from dokploy_wizard.state.sync_schema import SyncStateError

_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass(slots=True)  # noqa: MUTABLE_OK - records executed command arguments
class RecordingRunner:
    state_root: Path
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        self.calls.append(arguments)
        (self.state_root / "applied-state.json").write_text("after", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, stdout="ok\n", stderr="")


@dataclass(slots=True)  # noqa: MUTABLE_OK - mutates control fixtures during execution
class ChurningControlRunner:
    state_root: Path

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        (self.state_root / "applied-state.json").write_text("after", encoding="utf-8")
        (self.state_root / "lease-receipts" / "lease.json").write_text(
            "heartbeat-2",
            encoding="utf-8",
        )
        (self.state_root / "create-intents" / "lease.json").write_text(
            "control-2",
            encoding="utf-8",
        )
        (self.state_root / "runtime" / "lock_helper.py").write_text(
            "runtime-2",
            encoding="utf-8",
        )
        (self.state_root / "sync.lock").write_text("lock-2", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, stdout="ok\n", stderr="")


@dataclass(frozen=True, slots=True)
class CategorizedFailureRunner:
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr="DOKPLOY_WIZARD_SYNC_ERROR=catalog_source\n",
        )


@dataclass(frozen=True, slots=True)
class MissingRuntimeRunner:
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        return subprocess.CompletedProcess(
            arguments,
            2,
            stdout="",
            stderr="python: can't open file '/opt/dokploy-wizard/opencode_go_sync.py'\n",
        )


def test_production_executor_runs_exact_schedule_argv_and_env_and_reports_real_delta(
    tmp_path: Path,
) -> None:
    (tmp_path / "applied-state.json").write_text("before", encoding="utf-8")
    command = parse_immediate_sync_command(
        f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER} "
        "TZ=UTC python /app/scripts/reconcile_opencode_go.py --state-dir /state",
        owner_id=_OWNER,
        service_name="litellm",
        state_root=tmp_path,
    )
    runner = RecordingRunner(tmp_path)

    result = DockerExecImmediateSyncExecutor(runner=runner).execute(command)

    assert runner.calls == [
        (
            "docker",
            "exec",
            "--env",
            f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER}",
            "--env",
            "TZ=UTC",
            "litellm",
            "python",
            "/app/scripts/reconcile_opencode_go.py",
            "--state-dir",
            "/state",
        )
    ]
    assert result.before_snapshot_sha256 != result.after_snapshot_sha256
    assert result.durable_write_delta == ("applied-state.json",)


def test_production_snapshot_excludes_helper_control_churn(tmp_path: Path) -> None:
    for directory in ("lease-receipts", "create-intents", "runtime"):
        (tmp_path / directory).mkdir()
    (tmp_path / "applied-state.json").write_text("before", encoding="utf-8")
    (tmp_path / "lease-receipts" / "lease.json").write_text(
        "heartbeat-1",
        encoding="utf-8",
    )
    (tmp_path / "create-intents" / "lease.json").write_text(
        "control-1",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "lock_helper.py").write_text(
        "runtime-1",
        encoding="utf-8",
    )
    (tmp_path / "sync.lock").write_text("lock-1", encoding="utf-8")
    command = parse_immediate_sync_command(
        f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER} TZ=UTC python sync.py",
        owner_id=_OWNER,
        service_name="litellm",
        state_root=tmp_path,
    )

    result = DockerExecImmediateSyncExecutor(
        runner=ChurningControlRunner(tmp_path)
    ).execute(command)

    assert result.durable_write_delta == ("applied-state.json",)


def test_production_executor_preserves_only_fixed_runtime_failure_category(
    tmp_path: Path,
) -> None:
    command = parse_immediate_sync_command(
        f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER} TZ=UTC python sync.py",
        owner_id=_OWNER,
        service_name="litellm",
        state_root=tmp_path,
    )

    with pytest.raises(SyncStateError, match="catalog_source"):
        DockerExecImmediateSyncExecutor(runner=CategorizedFailureRunner()).execute(command)


def test_production_executor_classifies_missing_packaged_runtime(tmp_path: Path) -> None:
    command = parse_immediate_sync_command(
        f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={_OWNER} TZ=UTC python sync.py",
        owner_id=_OWNER,
        service_name="litellm",
        state_root=tmp_path,
    )

    with pytest.raises(SyncStateError, match="runtime_package_missing"):
        DockerExecImmediateSyncExecutor(runner=MissingRuntimeRunner()).execute(command)
