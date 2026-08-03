"""Canonical-wrapper observation capture around one remote Host A modify."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.remote_transport import (
    RemoteCommandCaptureLimits,
    RemoteOutputCallback,
    RemoteTransportSession,
)

BEFORE_OBSERVATION_SUBCOMMAND: Final = "modify-observation-before"
AFTER_OBSERVATION_SUBCOMMAND: Final = "modify-observation-after"
_CAPTURE_LIMITS: Final = RemoteCommandCaptureLimits(
    timeout_seconds=120.0,
    max_stdout_bytes=2 * 1024 * 1024,
    max_stderr_bytes=64 * 1024,
)


@dataclass(frozen=True, slots=True)
class RemoteModifyObservationRunner:
    """Run the activated release's read-only collector around lifecycle mutation."""

    session: RemoteTransportSession
    output_callback: RemoteOutputCallback

    def run(self, command: str, password: str | None) -> None:
        before = self._capture(BEFORE_OBSERVATION_SUBCOMMAND, password)
        self._emit(BEFORE_OBSERVATION_SUBCOMMAND, before)
        try:
            self.session.run_command(
                subcommand="modify",
                command=command,
                password=password,
            )
        finally:
            after = self._capture(AFTER_OBSERVATION_SUBCOMMAND, password)
            self._emit(AFTER_OBSERVATION_SUBCOMMAND, after)

    def _capture(self, subcommand: str, password: str | None) -> bytes:
        remote_root = self.session.remote_root
        active_release = self.session.remote_active_release_path
        command = " ".join(
            (
                "cd",
                shlex.quote(active_release),
                "&&",
                "env",
                "PYTHONPATH=src",
                "python3",
                "-m",
                "dokploy_wizard.proof.model_sync_upgrade_host_a_observation_collector",
                "--remote-root",
                shlex.quote(remote_root),
                "--state-dir",
                shlex.quote(self.session.remote_state_dir),
            )
        )
        return self.session.capture_command(
            subcommand=subcommand,
            command=command,
            limits=_CAPTURE_LIMITS,
            password=password,
        ).stdout

    def _emit(self, subcommand: str, payload: bytes) -> None:
        text = payload.decode("utf-8")
        for line in text.splitlines():
            self.output_callback(subcommand, "stdout", line)
