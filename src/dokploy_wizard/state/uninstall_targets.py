"""Provider-neutral physical targets used by uninstall authority receipts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class DokployComposeDeletionRecord:
    """Exact Dokploy compose target observed before uninstall."""

    compose_id: str
    project_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DockerVolumeRecord:
    """Exact Docker volume target observed before uninstall."""

    volume_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DockerNetworkRecord:
    """Exact Docker network target observed before uninstall."""

    network_id: str
    name: str


def dokploy_compose_fingerprint(compose: DokployComposeDeletionRecord) -> str:
    value = f"{compose.compose_id}\0{compose.project_id}\0{compose.name}"
    return sha256(value.encode()).hexdigest()


def docker_volume_fingerprint(volume: DockerVolumeRecord) -> str:
    return sha256(f"{volume.volume_id}\0{volume.name}".encode()).hexdigest()


def docker_network_fingerprint(network: DockerNetworkRecord) -> str:
    return sha256(f"{network.network_id}\0{network.name}".encode()).hexdigest()
