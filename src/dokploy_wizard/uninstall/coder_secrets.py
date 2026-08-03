"""Coder control-plane adapter for receipt-authorized secret destruction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dokploy_wizard.dokploy.coder import _coder_container_name, _coder_login, _service_name
from dokploy_wizard.dokploy.coder_secret_client import DockerExecCoderSecretClient
from dokploy_wizard.dokploy.coder_secret_destroy import (
    CoderSecretDestroyer,
    CoderSecretDestroyError,
)
from dokploy_wizard.dokploy.coder_secret_destroy_authorization import coder_secret_owner_id
from dokploy_wizard.state import DesiredState
from dokploy_wizard.uninstall.errors import UninstallExecutionError


@dataclass(frozen=True, slots=True)
class CoderSecretUninstaller:
    values: Mapping[str, str]
    state_dir: Path

    def destroy(self, desired_state: DesiredState) -> None:
        hostname = desired_state.hostnames.get("coder")
        email = self.values.get("DOKPLOY_ADMIN_EMAIL", "").strip()
        password = self.values.get("DOKPLOY_ADMIN_PASSWORD", "").strip()
        if hostname is None or email == "" or password == "":
            raise UninstallExecutionError("Coder secret destroy requires Coder admin credentials.")
        try:
            token = _coder_login(hostname=hostname, email=email, password=password)
            container_name = _coder_container_name(_service_name(desired_state.stack_name))
            if container_name is None:
                raise UninstallExecutionError(
                    "Coder container is unavailable for secret destroy."
                )
            CoderSecretDestroyer(
                state_dir=self.state_dir,
                client=DockerExecCoderSecretClient(
                    container_name=container_name,
                    session_token=token,
                    state_dir=self.state_dir,
                ),
                owner_id=coder_secret_owner_id(desired_state.stack_name, hostname),
            ).destroy()
        except CoderSecretDestroyError as error:
            raise UninstallExecutionError("Coder secret destroy failed.") from error
