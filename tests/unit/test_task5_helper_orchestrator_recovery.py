from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy import sync_helper_orchestrator
from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.dokploy.sync_helper_create import (
    CreateIntent,
    HelperContainerObservation,
)
from dokploy_wizard.dokploy.sync_helper_orchestrator import (
    ImmediateSyncConfig,
    run_immediate_sync_with_helper,
)
from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.dokploy.sync_helper_runtime import (
    DockerHelperRuntime,
    HelperLaunch,
)
from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.dokploy.sync_immediate_executor import (
    ImmediateSyncCommand,
    ImmediateSyncExecution,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
)
from dokploy_wizard.state.sync_schema import SyncStateError

_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_IMAGE = "ghcr.io/berriai/litellm@sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class VolumeRuntime:
    root: Path

    def volume_mountpoint(self, volume: str) -> Path:
        assert volume == "wizard-shared-litellm-data"
        return self.root

    def find(self, name: str) -> tuple[HelperContainerObservation, ...]:
        raise AssertionError(f"unexpected find: {name}")

    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation:
        raise AssertionError(f"unexpected create: {arguments}")

    def start(self, container_id: str) -> None:
        raise AssertionError(f"unexpected start: {container_id}")

    def remove(self, container_id: str) -> None:
        raise AssertionError(f"unexpected remove: {container_id}")


@dataclass(slots=True)  # noqa: MUTABLE_OK - records executed recovery command
class SuccessfulExecutor:
    commands: list[ImmediateSyncCommand] = field(default_factory=list)

    def execute(self, command: ImmediateSyncCommand) -> ImmediateSyncExecution:
        self.commands.append(command)
        return ImmediateSyncExecution(0, "1" * 64, "2" * 64, "3" * 64, ())


def _outcome() -> SyncScheduleOutcome:
    desired = SyncDesiredState.from_schedule(
        owner_id=_OWNER,
        config_sha256="b" * 64,
        litellm_image_digest=_IMAGE,
        metadata_volume="wizard-shared-litellm-data",
        schedule_spec=ScheduleSpec.for_shared_core(
            stack_name="wizard",
            compose_id="compose-1",
            owner_id=_OWNER,
        ),
    )
    return SyncScheduleOutcome(desired, AppliedSyncState.initial(desired), None)


def test_orchestrator_retries_one_dead_helper_with_the_same_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume_root = tmp_path / "volume"
    volume_root.mkdir()
    runtime = VolumeRuntime(volume_root)
    launches: list[str] = []
    removed: list[str] = []
    waits = 0

    def fake_launch(
        launch: HelperLaunch,
        *,
        runtime: DockerHelperRuntime,
    ) -> CreateIntent:
        del runtime
        assert launch.env_file.exists()
        launches.append(launch.request.lease)
        launch.env_file.unlink()
        return replace(
            CreateIntent.for_request(
                request=launch.request,
                stack=launch.stack,
                owner=launch.owner,
                image_digest=launch.image_digest,
                network=launch.network,
                volume_fingerprint="1" * 64,
                command_sha256="2" * 64,
            ),
            phase="created",
            container_id="full-container-id",
        )

    def fake_wait(path: Path, expected: set[str]) -> LeaseReceipt:
        nonlocal waits
        del path
        waits += 1
        request = captured_request[0]
        if waits == 1:
            raise SyncStateError("dead helper")
        receipt = replace(
            LeaseReceipt.created(request=request, container_id="full-container-id"),
            generation=4,
            receipt_version=4,
            phase="parent_running",
            heartbeat_at="2026-07-29T00:00:00+00:00",
            heartbeat_deadline_at="2026-07-29T00:00:05+00:00",
            lock_inode=123,
        )
        return replace(receipt, phase="released") if expected == {"released"} else receipt

    captured_request: list[LeaseRequest] = []
    original_stage = sync_helper_orchestrator._stage_helper_runtime

    def capture_stage(state_root: Path, request: LeaseRequest) -> None:
        captured_request.append(request)
        original_stage(state_root, request)

    def fake_remove(intent: CreateIntent, *, runtime: DockerHelperRuntime) -> None:
        del runtime
        removed.append(intent.container_id or "")

    monkeypatch.setattr(sync_helper_orchestrator, "_stage_helper_runtime", capture_stage)
    monkeypatch.setattr(sync_helper_orchestrator, "launch_sync_helper", fake_launch)
    monkeypatch.setattr(sync_helper_orchestrator, "_wait_for_phase", fake_wait)
    monkeypatch.setattr(sync_helper_orchestrator, "remove_sync_helper", fake_remove)

    result = run_immediate_sync_with_helper(
        ImmediateSyncConfig(
            wizard_state_dir=tmp_path / "wizard-state",
            stack="wizard",
            image_digest=_IMAGE,
            network="wizard-shared",
            metadata_volume="wizard-shared-litellm-data",
        ),
        _outcome(),
        executor=SuccessfulExecutor(),
        runtime=runtime,
    )

    assert result.status == "succeeded"
    assert len(launches) == 2
    assert launches[0] == launches[1]
    assert removed == ["full-container-id"]
