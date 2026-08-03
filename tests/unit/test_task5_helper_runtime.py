from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.sync_helper_create import CreateIntent, HelperContainerObservation
from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.dokploy.sync_helper_runtime import (
    DockerHelperRuntime,
    HelperLaunch,
    launch_sync_helper,
)
from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.state.sync_schema import SyncStateError
from dokploy_wizard.state.upgrade_io import atomic_json, read_json

_LEASE = "97099d6d-fd71-4ae0-8779-df6de483dfcc"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"
_IMAGE = "ghcr.io/berriai/litellm@sha256:" + "a" * 64


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _request() -> LeaseRequest:
    return LeaseRequest(
        lease=_LEASE,
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=123,
        parent_start_time_ticks=456,
        parent_argv_sha256="1" * 64,
        env=(("TZ", "2" * 64),),
        input_sha256="3" * 64,
        config_sha256="4" * 64,
        expected_state_sha256="5" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00Z",
    )


@dataclass
class FakeDockerRuntime(DockerHelperRuntime):
    matches: tuple[HelperContainerObservation, ...] = ()
    create_arguments: tuple[str, ...] | None = None
    started: list[str] = field(default_factory=list)
    state_dir: Path | None = None
    env_file: Path | None = None
    find_count: int = 0
    removed: list[str] = field(default_factory=list)
    remove_failures: int = 0

    def find(self, name: str) -> tuple[HelperContainerObservation, ...]:
        assert name == "wizard-opencode-go-lock-97099d6d"
        self.find_count += 1
        return self.matches

    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation:
        self.create_arguments = arguments
        observation = _observation("full-container-id")
        self.matches = (observation,)
        return observation

    def start(self, container_id: str) -> None:
        assert self.state_dir is not None and self.env_file is not None
        payload = read_json(
            self.state_dir / "create-intents" / f"{_LEASE}.json"
        )
        assert payload["container_id"] == container_id
        receipt = LeaseReceipt.from_dict(
            read_json(
                self.state_dir / "lease-receipts" / f"{_LEASE}.json",
                json_values=True,
            )
        )
        assert receipt.container_id == container_id
        assert not self.env_file.exists()
        self.started.append(container_id)

    def remove(self, container_id: str) -> None:
        if self.remove_failures:
            self.remove_failures -= 1
            raise SyncStateError("transient remove failure")
        self.removed.append(container_id)

    def volume_mountpoint(self, volume: str) -> Path:
        raise AssertionError(f"unexpected volume inspection: {volume}")


def _observation(container_id: str) -> HelperContainerObservation:
    command = (
        "python",
        "/state/runtime/lock_helper.py",
        "--request",
        f"/state/lease-requests/{_LEASE}.json",
        "--timeout-seconds",
        "300",
    )
    return HelperContainerObservation(
        container_id=container_id,
        name="wizard-opencode-go-lock-97099d6d",
        labels=(
            ("dokploy-wizard.stack", "wizard"),
            ("dokploy-wizard.owner", _OWNER),
            ("dokploy-wizard.lease", _LEASE),
        ),
        image_digest=_IMAGE,
        network="wizard-shared",
        volume_fingerprint=_digest("wizard-shared-litellm-data:/state"),
        command_sha256=_digest("\0".join(command)),
    )


def _launch(tmp_path: Path) -> HelperLaunch:
    env_file = tmp_path / "helper.env"
    env_file.write_text("TZ=UTC\n", encoding="utf-8")
    env_file.chmod(0o600)
    launch = HelperLaunch(
        request=_request(),
        state_dir=tmp_path,
        env_file=env_file,
        stack="wizard",
        owner=_OWNER,
        image_digest=_IMAGE,
        network="wizard-shared",
        metadata_volume="wizard-shared-litellm-data",
    )
    atomic_json(
        tmp_path / "lease-receipts" / f"{_LEASE}.json",
        LeaseReceipt.created(request=launch.request, container_id=None).to_dict(),
    )
    return launch


def test_helper_checkpoints_full_id_unlinks_env_then_starts(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    runtime = FakeDockerRuntime(state_dir=tmp_path, env_file=launch.env_file)

    result = launch_sync_helper(launch, runtime=runtime)

    assert result.container_id == "full-container-id"
    assert runtime.started == ["full-container-id"]
    assert runtime.create_arguments == (
        "create",
        "--restart=no",
        "--pid=host",
        "--name",
        "wizard-opencode-go-lock-97099d6d",
        "--network",
        "wizard-shared",
        "--env-file",
        str(launch.env_file),
        "--label",
        "dokploy-wizard.stack=wizard",
        "--label",
        f"dokploy-wizard.owner={_OWNER}",
        "--label",
        f"dokploy-wizard.lease={_LEASE}",
        "-v",
        "wizard-shared-litellm-data:/state",
        _IMAGE,
        "python",
        "/state/runtime/lock_helper.py",
        "--request",
        f"/state/lease-requests/{_LEASE}.json",
        "--timeout-seconds",
        "300",
    )


def test_helper_recovers_exact_after_create_before_id_checkpoint(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    runtime = FakeDockerRuntime(
        matches=(_observation("recovered-full-id"),),
        state_dir=tmp_path,
        env_file=launch.env_file,
    )

    result = launch_sync_helper(launch, runtime=runtime)

    assert result.container_id == "recovered-full-id"
    assert runtime.create_arguments is None
    assert runtime.started == ["recovered-full-id"]


def test_helper_fails_closed_on_multiple_recovery_matches(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    runtime = FakeDockerRuntime(
        matches=(_observation("first"), _observation("second")),
        state_dir=tmp_path,
        env_file=launch.env_file,
    )

    with pytest.raises(SyncStateError, match="Multiple"):
        launch_sync_helper(launch, runtime=runtime)

    assert not launch.env_file.exists()
    assert runtime.started == []


def test_helper_rejects_nonexact_existing_intent_before_docker_mutation(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    intent = CreateIntent.for_request(
        request=launch.request,
        stack=launch.stack,
        owner=launch.owner,
        image_digest=launch.image_digest,
        network="foreign-network",
        volume_fingerprint=_digest(f"{launch.metadata_volume}:/state"),
        command_sha256=_observation("unused").command_sha256,
    )
    atomic_json(
        tmp_path / "create-intents" / f"{_LEASE}.json",
        intent.to_dict(),
    )
    runtime = FakeDockerRuntime(state_dir=tmp_path, env_file=launch.env_file)

    with pytest.raises(SyncStateError, match="exact pre-create intent"):
        launch_sync_helper(launch, runtime=runtime)

    assert runtime.find_count == 0
    assert runtime.create_arguments is None

