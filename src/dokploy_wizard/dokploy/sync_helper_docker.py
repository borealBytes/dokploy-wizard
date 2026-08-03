"""Docker CLI adapter and strict helper-container observation parser."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

from dokploy_wizard.dokploy.sync_helper_create import HelperContainerObservation
from dokploy_wizard.state.sync_schema import SyncStateError


class SubprocessDockerHelperRuntime:
    """Docker CLI adapter used by the production Shared Core backend."""

    def find(self, name: str) -> tuple[HelperContainerObservation, ...]:
        result = _run_docker(("ps", "-aq", "--filter", f"name=^/{name}$"))
        identifiers = tuple(line for line in result.splitlines() if line)
        return tuple(self._inspect(identifier) for identifier in identifiers)

    def create(self, arguments: tuple[str, ...]) -> HelperContainerObservation:
        container_id = _run_docker(arguments).strip()
        if container_id == "":
            raise SyncStateError("Docker create returned no helper container ID.")
        return self._inspect(container_id)

    def start(self, container_id: str) -> None:
        _run_docker(("start", container_id))

    def remove(self, container_id: str) -> None:
        _run_docker(("rm", "-f", container_id))

    def volume_mountpoint(self, volume: str) -> Path:
        value = _run_docker(("volume", "inspect", volume, "--format", "{{.Mountpoint}}"))
        path = Path(value.strip())
        if not path.is_absolute() or not path.is_dir():
            raise SyncStateError("Docker metadata volume mountpoint is invalid.")
        return path

    def _inspect(self, identifier: str) -> HelperContainerObservation:
        raw = _run_docker(("inspect", identifier))
        payload = json.loads(raw)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise SyncStateError("Docker inspect returned an invalid helper payload.")
        record = payload[0]
        config = record.get("Config")
        networks = record.get("NetworkSettings")
        mounts = record.get("Mounts")
        if (
            not isinstance(config, dict)
            or not isinstance(networks, dict)
            or not isinstance(mounts, list)
        ):
            raise SyncStateError("Docker helper observation is incomplete.")
        return _parse_observation(record, config, networks, mounts)


def _parse_observation(
    record: dict[str, object],
    config: dict[str, object],
    network_settings: dict[str, object],
    mounts: list[object],
) -> HelperContainerObservation:
    labels = config.get("Labels")
    command = config.get("Cmd")
    image = config.get("Image")
    network_map = network_settings.get("Networks")
    mount = next(
        (item for item in mounts if isinstance(item, dict) and item.get("Destination") == "/state"),
        None,
    )
    if not isinstance(labels, dict) or not isinstance(command, list) or not all(
        isinstance(item, str) for item in command
    ):
        raise SyncStateError("Docker helper config is invalid.")
    if not isinstance(image, str) or not isinstance(network_map, dict) or len(network_map) != 1:
        raise SyncStateError("Docker helper image or network is invalid.")
    if not isinstance(mount, dict) or not isinstance(mount.get("Name"), str):
        raise SyncStateError("Docker helper state volume is invalid.")
    container_id = record.get("Id")
    name = record.get("Name")
    if not isinstance(container_id, str) or not isinstance(name, str):
        raise SyncStateError("Docker helper identity is invalid.")
    label_names = (
        "dokploy-wizard.stack",
        "dokploy-wizard.owner",
        "dokploy-wizard.lease",
    )
    if any(not isinstance(labels.get(label), str) for label in label_names):
        raise SyncStateError("Docker helper ownership labels are invalid.")
    required = tuple((label, str(labels[label])) for label in label_names)
    return HelperContainerObservation(
        container_id=container_id,
        name=name.removeprefix("/"),
        labels=required,
        image_digest=image,
        network=next(iter(network_map)),
        volume_fingerprint=_digest(f"{mount['Name']}:/state"),
        command_sha256=_digest("\0".join(command)),
    )


def _run_docker(arguments: tuple[str, ...]) -> str:
    result = subprocess.run(
        ("docker", *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SyncStateError(f"Docker helper command failed: {result.stderr.strip()}")
    return result.stdout


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
