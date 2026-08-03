"""Parent-side helper lease orchestration around immediate LiteLLM reconciliation."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from dokploy_wizard.dokploy import (
    lock_helper_io,
    lock_helper_protocol,
    lock_helper_protocol_base,
    lock_helper_protocol_validation,
    lock_helper_runtime,
)
from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.dokploy.sync_helper_identity import read_parent_identity
from dokploy_wizard.dokploy.sync_helper_lease import LeaseRelease, LeaseResult
from dokploy_wizard.dokploy.sync_helper_receipt import LeaseReceipt
from dokploy_wizard.dokploy.sync_helper_runtime import (
    DockerHelperRuntime,
    HelperLaunch,
    SubprocessDockerHelperRuntime,
    launch_sync_helper,
    remove_sync_helper,
)
from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.dokploy.sync_immediate_executor import (
    DockerExecImmediateSyncExecutor,
    ImmediateSyncExecutor,
    parse_immediate_sync_command,
)
from dokploy_wizard.state.sync_schema import SyncStateError, canonical_digest
from dokploy_wizard.state.upgrade_io import atomic_bytes
from dokploy_wizard.state.upgrade_io import atomic_json as atomic_json


@dataclass(frozen=True, slots=True)
class ImmediateSyncConfig:
    wizard_state_dir: Path
    stack: str
    image_digest: str
    network: str
    metadata_volume: str


def run_immediate_sync_with_helper(
    config: ImmediateSyncConfig,
    outcome: SyncScheduleOutcome,
    *,
    executor: ImmediateSyncExecutor | None = None,
    runtime: DockerHelperRuntime | None = None,
) -> LeaseResult:
    """Hold the helper lease while executing the exact owner-marked sync command."""

    docker = runtime or SubprocessDockerHelperRuntime()
    immediate_executor = executor or DockerExecImmediateSyncExecutor()
    state_root = docker.volume_mountpoint(config.metadata_volume)
    lease = str(uuid.uuid4())
    parent = read_parent_identity(os.getpid())
    owner_env = {
        "DOKPLOY_WIZARD_SCHEDULE_OWNER_ID": outcome.desired.owner_id,
        "TZ": "UTC",
    }
    request = LeaseRequest(
        lease=lease,
        generation=1,
        receipt_version=1,
        mode="reconcile" if outcome.desired.enabled else "disable",
        parent_pid=parent.pid,
        parent_start_time_ticks=parent.start_time_ticks,
        parent_argv_sha256=parent.argv_sha256,
        env=tuple(
            sorted((name, _digest(value)) for name, value in owner_env.items())
        ),
        input_sha256=outcome.desired.fingerprint(),
        config_sha256=outcome.desired.config_sha256,
        expected_state_sha256=canonical_digest(outcome.applied.to_dict()),
        tombstone_sha256=outcome.applied.disable_tombstone_sha256,
        created_at=_now(),
    )
    _stage_helper_runtime(state_root, request)
    env_file = _write_ephemeral_env(config.wizard_state_dir, lease, owner_env)
    launch = HelperLaunch(
            request=request,
            state_dir=state_root,
            env_file=env_file,
            stack=config.stack,
            owner=outcome.desired.owner_id,
            image_digest=config.image_digest,
            network=config.network,
            metadata_volume=config.metadata_volume,
    )
    intent = None
    try:
        receipt_path = state_root / "lease-receipts" / f"{lease}.json"
        held: LeaseReceipt | None = None
        for attempt in range(2):
            if attempt == 1:
                launch = replace(
                    launch,
                    env_file=_write_ephemeral_env(
                        config.wizard_state_dir,
                        lease,
                        owner_env,
                    ),
                )
            intent = launch_sync_helper(launch, runtime=docker)
            try:
                held = _wait_for_phase(receipt_path, {"parent_running"})
            except SyncStateError:
                if attempt == 1:
                    raise
            else:
                break
        if held is None:
            raise SyncStateError("External helper recovery did not acquire the lease.")
        started_at = _now()
        execution = immediate_executor.execute(
            parse_immediate_sync_command(
                outcome.desired.schedule_spec.command,
                owner_id=outcome.desired.owner_id,
                service_name=outcome.desired.schedule_spec.service_name,
                state_root=state_root,
            )
        )
        result = LeaseResult(
            lease=lease,
            generation=held.generation,
            request_sha256=request.sha256(),
            status="succeeded",
            parent_exit_code=execution.parent_exit_code,
            before_snapshot_sha256=execution.before_snapshot_sha256,
            after_snapshot_sha256=execution.after_snapshot_sha256,
            parent_reconcile_sha256=execution.parent_reconcile_sha256,
            durable_write_delta=execution.durable_write_delta,
            started_at=started_at,
            ended_at=_now(),
        )
        atomic_json(state_root / "lease-results" / f"{lease}.json", result.to_dict())
        release = LeaseRelease(
            lease=lease,
            generation=held.generation,
            expected_receipt_version=held.receipt_version,
            expected_result_sha256=result.sha256(),
            requested_at=_now(),
        )
        atomic_json(state_root / "lease-releases" / f"{lease}.json", release.to_dict())
        _wait_for_phase(receipt_path, {"released"})
        return result
    finally:
        launch.env_file.unlink(missing_ok=True)
        if intent is not None:
            remove_sync_helper(intent, runtime=docker)


def _stage_helper_runtime(state_root: Path, request: LeaseRequest) -> None:
    source = Path(lock_helper_runtime.__file__).read_bytes()
    runtime_path = state_root / "runtime" / "lock_helper.py"
    atomic_bytes(runtime_path, source)
    protocol_source = Path(lock_helper_protocol.__file__).read_bytes()
    atomic_bytes(state_root / "runtime" / "lock_helper_protocol.py", protocol_source)
    base_source = Path(lock_helper_protocol_base.__file__).read_bytes()
    atomic_bytes(state_root / "runtime" / "lock_helper_protocol_base.py", base_source)
    validation_source = Path(lock_helper_protocol_validation.__file__).read_bytes()
    atomic_bytes(
        state_root / "runtime" / "lock_helper_protocol_validation.py",
        validation_source,
    )
    io_source = Path(lock_helper_io.__file__).read_bytes()
    atomic_bytes(state_root / "runtime" / "lock_helper_io.py", io_source)
    atomic_json(
        state_root / "lease-requests" / f"{request.lease}.json",
        request.to_dict(),
    )
    atomic_json(
        state_root / "lease-receipts" / f"{request.lease}.json",
        LeaseReceipt.created(request=request, container_id=None).to_dict(),
    )


def _write_ephemeral_env(
    state_dir: Path,
    lease: str,
    values: Mapping[str, str],
) -> Path:
    path = state_dir / "sync-helper-env" / f"{lease}.env"
    encoded = "".join(f"{name}={value}\n" for name, value in sorted(values.items())).encode()
    atomic_bytes(path, encoded)
    return path


def _wait_for_phase(path: Path, expected: set[str]) -> LeaseReceipt:
    deadline = time.monotonic() + 10
    while time.monotonic() <= deadline:
        if path.exists():
            receipt = LeaseReceipt.from_dict(lock_helper_io.read_json(path))
            if receipt.phase in expected:
                return receipt
            if receipt.phase == "failed":
                raise SyncStateError(f"External helper failed: {receipt.error}")
        time.sleep(0.05)
    raise SyncStateError("External helper did not reach its expected phase.")


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
