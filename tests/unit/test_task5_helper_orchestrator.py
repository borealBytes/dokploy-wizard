from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import pytest

from dokploy_wizard.dokploy import sync_helper_orchestrator
from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.dokploy.sync_helper_create import HelperContainerObservation
from dokploy_wizard.dokploy.sync_helper_orchestrator import (
    ImmediateSyncConfig,
    run_immediate_sync_with_helper,
)
from dokploy_wizard.dokploy.sync_immediate_executor import (
    ImmediateSyncCommand,
    ImmediateSyncExecution,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
)
from dokploy_wizard.state.sync_schema import JsonValue, SyncStateError

_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_IMAGE = "ghcr.io/berriai/litellm@sha256:" + "a" * 64
_CONTAINER_ID = "f" * 64


class CommandExecutor(Protocol):
    commands: list[ImmediateSyncCommand]


@dataclass(slots=True)  # noqa: MUTABLE_OK - records process-backed test observations
class RecordingExecutor:
    commands: list[ImmediateSyncCommand] = field(default_factory=list)
    failure: RuntimeError | None = None

    def execute(self, command: ImmediateSyncCommand) -> ImmediateSyncExecution:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return ImmediateSyncExecution(
            parent_exit_code=0,
            before_snapshot_sha256="1" * 64,
            after_snapshot_sha256="2" * 64,
            parent_reconcile_sha256="3" * 64,
            durable_write_delta=("opencode-go/state.json",),
        )


@dataclass(slots=True)  # noqa: MUTABLE_OK - owns a test subprocess and observations
class ProcessBackedDockerRuntime:
    root: Path
    process: subprocess.Popen[str] | None = None
    removed: list[str] = field(default_factory=list)
    observation: HelperContainerObservation | None = None

    def volume_mountpoint(self, volume: str) -> Path:
        assert volume == "wizard-shared-litellm-data"
        return self.root

    def find(self, name: str) -> tuple[HelperContainerObservation, ...]:
        if self.observation is None:
            return ()
        assert self.observation.name == name
        return (self.observation,)

    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation:
        name = arguments[arguments.index("--name") + 1]
        network = arguments[arguments.index("--network") + 1]
        volume = arguments[arguments.index("-v") + 1]
        labels = tuple(
            (value.split("=", 1)[0], value.split("=", 1)[1])
            for index, value in enumerate(arguments)
            if index > 0 and arguments[index - 1] == "--label"
        )
        image_index = arguments.index("--entrypoint") + 2
        image = arguments[image_index]
        command = arguments[image_index + 1 :]
        self.observation = HelperContainerObservation(
            container_id=_CONTAINER_ID,
            name=name,
            labels=labels,
            image_digest=image,
            network=network,
            volume_fingerprint=_digest(volume),
            command_sha256=_digest("\0".join(command)),
        )
        return self.observation

    def start(self, container_id: str) -> None:
        assert container_id == _CONTAINER_ID
        intent_dir = self.root / "create-intents"
        request = next((self.root / "lease-requests").glob("*.json"))
        assert next(intent_dir.glob("*.json")).exists()
        self.process = subprocess.Popen(
            (
                sys.executable,
                str(self.root / "runtime" / "lock_helper.py"),
                "--request",
                str(request),
                "--timeout-seconds",
                "5",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def remove(self, container_id: str) -> None:
        assert self.process is not None
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.removed.append(container_id)


def _outcome() -> SyncScheduleOutcome:
    schedule = ScheduleSpec.for_shared_core(
        stack_name="wizard",
        compose_id="compose-1",
        owner_id=_OWNER,
    )
    desired = SyncDesiredState.from_schedule(
        owner_id=_OWNER,
        config_sha256="b" * 64,
        litellm_image_digest=_IMAGE,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=schedule,
    )
    return SyncScheduleOutcome(
        desired=desired,
        applied=AppliedSyncState.initial(desired),
        metadata=None,
    )


def test_parent_reconciliation_runs_while_external_helper_holds_lock(tmp_path: Path) -> None:
    volume_root = tmp_path / "volume"
    volume_root.mkdir()
    runtime = ProcessBackedDockerRuntime(volume_root)
    executor = RecordingExecutor()

    result = run_immediate_sync_with_helper(
        ImmediateSyncConfig(
            wizard_state_dir=tmp_path / "wizard-state",
            stack="wizard",
            image_digest=_IMAGE,
            network="wizard-shared",
            metadata_volume="wizard-shared-litellm-data",
        ),
        _outcome(),
        executor=executor,
        runtime=runtime,
    )

    assert result.status == "succeeded"
    assert len(executor.commands) == 1
    assert executor.commands[0].env == (
        ("DOKPLOY_WIZARD_SCHEDULE_OWNER_ID", _OWNER),
        ("TZ", "UTC"),
    )
    assert executor.commands[0].argv == tuple(
        _outcome().desired.schedule_spec.command.split()[2:]
    )
    runtime_package = volume_root / "opencode_go_sync.py"
    runtime_config = volume_root / "opencode-go.json"
    assert runtime_package.is_file()
    assert runtime_config.is_file()
    with zipfile.ZipFile(runtime_package) as archive:
        assert "__main__.py" in archive.namelist()
        assert "dokploy_wizard/litellm/opencode_go_sync_runtime.py" in archive.namelist()
    package_help = subprocess.run(
        (sys.executable, str(runtime_package), "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert package_help.returncode == 0
    assert json.loads(runtime_config.read_text(encoding="utf-8"))["owner_id"] == _OWNER
    assert runtime.removed == [_CONTAINER_ID]
    assert not tuple((tmp_path / "wizard-state" / "sync-helper-env").glob("*.env"))


def test_reconcile_failure_still_removes_exact_helper(tmp_path: Path) -> None:
    volume_root = tmp_path / "volume"
    volume_root.mkdir()
    runtime = ProcessBackedDockerRuntime(volume_root)

    with pytest.raises(RuntimeError, match="reconcile failed"):
        run_immediate_sync_with_helper(
            ImmediateSyncConfig(
                wizard_state_dir=tmp_path / "wizard-state",
                stack="wizard",
                image_digest=_IMAGE,
                network="wizard-shared",
                metadata_volume="wizard-shared-litellm-data",
            ),
            _outcome(),
            executor=RecordingExecutor(failure=RuntimeError("reconcile failed")),
            runtime=runtime,
        )

    assert runtime.removed == [_CONTAINER_ID]
    assert not tuple((tmp_path / "wizard-state" / "sync-helper-env").glob("*.env"))


@pytest.mark.parametrize("failure_phase", ["wait", "result", "release"])
def test_post_create_phase_failure_always_removes_exact_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    volume_root = tmp_path / "volume"
    volume_root.mkdir()
    runtime = ProcessBackedDockerRuntime(volume_root)
    if failure_phase == "wait":
        monkeypatch.setattr(
            sync_helper_orchestrator,
            "_wait_for_phase",
            lambda *_: (_ for _ in ()).throw(SyncStateError("wait failed")),
        )
    else:
        persist = sync_helper_orchestrator.atomic_json

        def fail_selected(path: Path, payload: dict[str, JsonValue]) -> None:
            if failure_phase == "result" and path.parent.name == "lease-results":
                raise OSError("result write failed")
            if failure_phase == "release" and path.parent.name == "lease-releases":
                raise OSError("release write failed")
            persist(path, payload)

        monkeypatch.setattr(sync_helper_orchestrator, "atomic_json", fail_selected)

    with pytest.raises((OSError, SyncStateError)):
        run_immediate_sync_with_helper(
            ImmediateSyncConfig(
                wizard_state_dir=tmp_path / "wizard-state",
                stack="wizard",
                image_digest=_IMAGE,
                network="wizard-shared",
                metadata_volume="wizard-shared-litellm-data",
            ),
            _outcome(),
            executor=RecordingExecutor(),
            runtime=runtime,
        )

    assert runtime.removed == [_CONTAINER_ID]


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()
