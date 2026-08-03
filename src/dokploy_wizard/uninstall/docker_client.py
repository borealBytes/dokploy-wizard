"""Process-backed Docker read and deletion capability for uninstall authority."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.state.uninstall_targets import DockerNetworkRecord, DockerVolumeRecord
from dokploy_wizard.uninstall.errors import UninstallExecutionError

_INSPECT_TIMEOUT_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class _DockerInspectFound:
    name: str


@dataclass(frozen=True, slots=True)
class _DockerInspectAbsent:
    pass


DockerInspectOutcome = _DockerInspectFound | _DockerInspectAbsent


class SubprocessDockerDeletionClient:
    """Read and remove exact Docker volume or network targets."""

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None:
        outcome = self._inspect("volume", volume_id)
        match outcome:
            case _DockerInspectFound(name=name):
                return DockerVolumeRecord(volume_id=volume_id, name=name)
            case _DockerInspectAbsent():
                return None

    def delete_volume(self, volume_id: str) -> None:
        self._run("volume", "rm", volume_id)

    def get_network(self, network_id: str) -> DockerNetworkRecord | None:
        outcome = self._inspect("network", network_id)
        match outcome:
            case _DockerInspectFound(name=name):
                return DockerNetworkRecord(network_id=network_id, name=name)
            case _DockerInspectAbsent():
                return None

    def delete_network(self, network_id: str) -> None:
        self._run("network", "rm", network_id)

    def _inspect(self, kind: str, target_id: str) -> DockerInspectOutcome:
        try:
            result = subprocess.run(
                ["docker", kind, "inspect", target_id, "--format", "{{.Name}}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=_INSPECT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UninstallExecutionError("Docker inspection failed.") from error
        if result.returncode != 0:
            if _is_confirmed_absence(result, kind, target_id):
                return _DockerInspectAbsent()
            raise UninstallExecutionError("Docker inspection failed.")
        name = result.stdout.strip()
        if name == "":
            raise UninstallExecutionError("Docker inspection returned an empty target name.")
        return _DockerInspectFound(name)

    def _run(self, kind: str, operation: str, target_id: str) -> None:
        result = subprocess.run(
            ["docker", kind, operation, target_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise UninstallExecutionError("Docker deletion command failed.")


def _is_confirmed_absence(
    result: subprocess.CompletedProcess[str], kind: str, target_id: str
) -> bool:
    """Recognize only Docker's exact target-not-found diagnostics."""

    expected = {
        f"Error response from daemon: get {kind} {target_id}: no such {kind}",
        f"Error response from daemon: network {target_id} not found",
    }
    return result.stdout == "" and result.stderr.strip() in expected
