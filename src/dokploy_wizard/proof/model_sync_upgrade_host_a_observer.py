"""Read-only SSH source for Host A authoritative observations."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    HostAObservation,
    parse_host_a_observation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport


@dataclass(frozen=True, slots=True, repr=False)
class RemoteHostAObserver:
    host: str
    password: str
    remote_root: Path = Path("/root/dokploy-wizard")

    def capture(self) -> HostAObservation:
        command = "cd {} && {}".format(
            shlex.quote(str(self.remote_root / "current")),
            shlex.join(
                (
                    "env",
                    "PYTHONPATH=src",
                    "python3",
                    "-m",
                    "dokploy_wizard.proof.model_sync_upgrade_host_a_observation_collector",
                    "--remote-root",
                    str(self.remote_root),
                    "--state-dir",
                    str(self.remote_root / "state"),
                )
            ),
        )
        try:
            transport = ParamikoRemoteTransport.connect(
                hostname=self.host,
                username="root",
                password=self.password,
                remote_root=str(self.remote_root),
            )
            try:
                raw = capture_remote_output(
                    transport,
                    command,
                    timeout_seconds=120,
                )
            finally:
                transport.close()
        except (OSError, RuntimeError, ValueError) as error:
            raise UpgradeHostAError("Host A remote observation failed") from error
        try:
            decoded: proof.JsonValue = json.loads(raw)
            payload = proof.require_mapping(decoded, "Host A observation")
        except (json.JSONDecodeError, ValueError) as error:
            raise UpgradeHostAError("Host A remote observation is malformed") from error
        return parse_host_a_observation(payload, remote_root=self.remote_root)
