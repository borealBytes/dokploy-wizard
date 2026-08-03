from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.sync_helper_create import HelperContainerObservation
from dokploy_wizard.dokploy.sync_helper_identity import read_parent_identity
from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.dokploy.sync_helper_runtime import (
    HelperLaunch,
    launch_sync_helper,
    remove_sync_helper,
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
    parent = read_parent_identity(os.getpid())
    return LeaseRequest(
        lease=_LEASE,
        generation=1,
        receipt_version=1,
        mode="reconcile",
        parent_pid=parent.pid,
        parent_start_time_ticks=parent.start_time_ticks,
        parent_argv_sha256=parent.argv_sha256,
        env=(("TZ", "2" * 64),),
        input_sha256="3" * 64,
        config_sha256="4" * 64,
        expected_state_sha256="5" * 64,
        tombstone_sha256=None,
        created_at="2026-07-28T00:00:00Z",
    )


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


@dataclass(slots=True)  # noqa: MUTABLE_OK - simulates mutable container runtime state
class RecoveryRuntime:
    matches: tuple[HelperContainerObservation, ...] = ()
    removed: list[str] = field(default_factory=list)
    remove_failures: int = 0
    started: list[str] = field(default_factory=list)

    def find(self, name: str) -> tuple[HelperContainerObservation, ...]:
        assert name == "wizard-opencode-go-lock-97099d6d"
        return self.matches

    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation:
        del arguments
        observation = _observation("full-container-id")
        self.matches = (observation,)
        return observation

    def start(self, container_id: str) -> None:
        self.started.append(container_id)

    def remove(self, container_id: str) -> None:
        if self.remove_failures:
            self.remove_failures -= 1
            raise SyncStateError("transient remove failure")
        self.removed.append(container_id)

    def volume_mountpoint(self, volume: str) -> Path:
        raise AssertionError(f"unexpected volume inspection: {volume}")


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


def test_helper_recovers_stale_same_lease_after_dead_helper_os_unlock(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    first_runtime = RecoveryRuntime()
    created = launch_sync_helper(launch, runtime=first_runtime)
    receipt_path = tmp_path / "lease-receipts" / f"{_LEASE}.json"
    receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    atomic_json(
        receipt_path,
        replace(
            receipt,
            phase="parent_running",
            heartbeat_at="2026-07-28T00:00:00+00:00",
            heartbeat_deadline_at="2026-07-28T00:00:01+00:00",
            lock_inode=123,
        ).to_dict(),
    )
    retry_env = tmp_path / "retry.env"
    retry_env.write_text("TZ=UTC\n", encoding="utf-8")
    retry_env.chmod(0o600)
    retry_runtime = RecoveryRuntime(matches=(_observation(created.container_id or ""),))

    recovered = launch_sync_helper(
        replace(launch, env_file=retry_env),
        runtime=retry_runtime,
    )

    recovered_receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    assert recovered.container_id == created.container_id
    assert recovered_receipt.phase == "created"
    assert recovered_receipt.generation == receipt.generation + 1
    assert retry_runtime.started == [created.container_id]


def test_post_create_receipt_failure_unlinks_env_and_removes_exact_helper(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    foreign_request = replace(launch.request, config_sha256="9" * 64)
    atomic_json(
        tmp_path / "lease-receipts" / f"{_LEASE}.json",
        LeaseReceipt.created(request=foreign_request, container_id=None).to_dict(),
    )
    runtime = RecoveryRuntime(remove_failures=1)

    with pytest.raises(SyncStateError, match="exact lease"):
        launch_sync_helper(launch, runtime=runtime)

    assert not launch.env_file.exists()
    assert runtime.started == []
    assert runtime.removed == ["full-container-id"]


def test_stale_same_lease_is_not_recovered_while_os_lock_is_held(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    runtime = RecoveryRuntime()
    created = launch_sync_helper(launch, runtime=runtime)
    receipt_path = tmp_path / "lease-receipts" / f"{_LEASE}.json"
    receipt = LeaseReceipt.from_dict(read_json(receipt_path, json_values=True))
    atomic_json(
        receipt_path,
        replace(
            receipt,
            phase="parent_running",
            heartbeat_at="2026-07-28T00:00:00+00:00",
            heartbeat_deadline_at="2026-07-28T00:00:01+00:00",
            lock_inode=123,
        ).to_dict(),
    )
    retry_env = tmp_path / "retry.env"
    retry_env.write_text("TZ=UTC\n", encoding="utf-8")
    retry_env.chmod(0o600)
    retry_runtime = RecoveryRuntime(matches=(_observation(created.container_id or ""),))
    descriptor = os.open(tmp_path / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SyncStateError, match="still owns the OS lock"):
            launch_sync_helper(
                replace(launch, env_file=retry_env),
                runtime=retry_runtime,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert retry_runtime.started == []
    assert retry_runtime.removed == []


def test_cleanup_never_removes_an_unmatched_container(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    runtime = RecoveryRuntime()
    created = launch_sync_helper(launch, runtime=runtime)
    runtime.matches = (replace(_observation("full-container-id"), network="foreign"),)

    with pytest.raises(SyncStateError, match="exact create intent"):
        remove_sync_helper(created, runtime=runtime)

    assert runtime.removed == []
