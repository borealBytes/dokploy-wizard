"""Executable crash-safe Docker create/start path for the external sync helper."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from dokploy_wizard.dokploy.sync_helper_create import (
    CreateIntent,
    HelperContainerObservation,
    bind_created_container,
)
from dokploy_wizard.dokploy.sync_helper_docker import (
    SubprocessDockerHelperRuntime as SubprocessDockerHelperRuntime,
)
from dokploy_wizard.dokploy.sync_helper_recovery import bind_or_recover_lease_receipt
from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest
from dokploy_wizard.state.sync_schema import SyncStateError
from dokploy_wizard.state.upgrade_io import atomic_json, read_json


class DockerHelperRuntime(Protocol):
    def find(self, name: str) -> tuple[HelperContainerObservation, ...]: ...
    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation: ...
    def start(self, container_id: str) -> None: ...
    def remove(self, container_id: str) -> None: ...
    def volume_mountpoint(self, volume: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class HelperLaunch:
    request: LeaseRequest
    state_dir: Path
    env_file: Path
    stack: str
    owner: str
    image_digest: str
    network: str
    metadata_volume: str


def launch_sync_helper(
    launch: HelperLaunch,
    *,
    runtime: DockerHelperRuntime,
) -> CreateIntent:
    """Checkpoint intent and full container ID before unlinking env and starting."""

    command = (
        "python",
        "/state/runtime/lock_helper.py",
        "--request",
        f"/state/lease-requests/{launch.request.lease}.json",
        "--timeout-seconds",
        "300",
    )
    volume_fingerprint = _digest(f"{launch.metadata_volume}:/state")
    command_sha256 = _digest("\0".join(command))
    expected = CreateIntent.for_request(
        request=launch.request,
        stack=launch.stack,
        owner=launch.owner,
        image_digest=launch.image_digest,
        network=launch.network,
        volume_fingerprint=volume_fingerprint,
        command_sha256=command_sha256,
    )
    intent_path = launch.state_dir / "create-intents" / f"{launch.request.lease}.json"
    created_this_attempt = not intent_path.exists()
    intent_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    intent = _load_or_persist_intent(intent_path, expected)
    arguments = _create_arguments(launch, intent, command)
    created: CreateIntent | None = None
    receipt_bound = False
    started = False
    try:
        created = bind_created_container(
            intent,
            matches=runtime.find(intent.name),
            create_container=lambda: runtime.create(arguments),
            persist_created=lambda value: atomic_json(intent_path, value.to_dict()),
        )
        if created.container_id is None:
            raise SyncStateError("Created helper intent is missing its full container ID.")
        receipt_path = launch.state_dir / "lease-receipts" / f"{launch.request.lease}.json"
        bind_or_recover_lease_receipt(
            path=receipt_path,
            request=launch.request,
            container_id=created.container_id,
            lock_path=launch.state_dir / "sync.lock",
        )
        receipt_bound = True
        launch.env_file.unlink()
        runtime.start(created.container_id)
        started = True
        return created
    finally:
        launch.env_file.unlink(missing_ok=True)
        if (
            not started
            and created is not None
            and (created_this_attempt or receipt_bound)
        ):
            remove_sync_helper(created, runtime=runtime)


def remove_sync_helper(intent: CreateIntent, *, runtime: DockerHelperRuntime) -> None:
    """Remove only the exact full-ID helper bound by a durable created intent."""

    if intent.container_id is None:
        return
    matches = runtime.find(intent.name)
    if not matches:
        return
    bind_created_container(
        intent,
        matches=matches,
        create_container=lambda: matches[0],
        persist_created=lambda _: None,
    )
    for attempt in range(3):
        try:
            runtime.remove(intent.container_id)
        except SyncStateError:
            if attempt == 2:
                raise
        else:
            return


def _create_arguments(
    launch: HelperLaunch,
    intent: CreateIntent,
    command: tuple[str, ...],
) -> tuple[str, ...]:
    labels = tuple(item for pair in intent.labels for item in ("--label", "=".join(pair)))
    return (
        "create",
        "--restart=no",
        "--pid=host",
        "--name",
        intent.name,
        "--network",
        launch.network,
        "--env-file",
        str(launch.env_file),
        *labels,
        "-v",
        f"{launch.metadata_volume}:/state",
        launch.image_digest,
        *command,
    )


def _load_or_persist_intent(path: Path, expected: CreateIntent) -> CreateIntent:
    if not path.exists():
        atomic_json(path, expected.to_dict())
        return expected
    intent = CreateIntent.from_dict(read_json(path, json_values=True))
    if intent.phase == "create_intent" and intent != expected:
        raise SyncStateError("Existing helper intent is not the exact pre-create intent.")
    if intent.phase == "created":
        recovered_pre_create = replace(
            intent,
            phase="create_intent",
            container_id=None,
            receipt_version=expected.receipt_version,
        )
        if recovered_pre_create != expected:
            raise SyncStateError("Existing helper intent is not the exact pre-create intent.")
    return intent


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
