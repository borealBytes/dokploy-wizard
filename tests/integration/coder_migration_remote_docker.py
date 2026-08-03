from __future__ import annotations

import json
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from tests.integration.coder_migration_remote_docker_metadata import (
    parse_loopback_port,
)
from tests.integration.coder_migration_remote_docker_metadata import (
    socket_group_id as read_socket_group_id,
)
from tests.integration.coder_migration_remote_http import RemoteCoderEndpoint
from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError
from tests.integration.coder_migration_remote_runtime_probe import WorkspaceRuntimeProbe
from tests.integration.coder_migration_remote_template import WorkspaceTemplateAddress

_PROVISIONER_READY_ATTEMPTS: Final = 120
_PROVISIONER_STABLE_OBSERVATIONS: Final = 2
_BUILTIN_PROVISIONER_DAEMONS: Final = 3


@dataclass(frozen=True, slots=True)
class RuntimeImages:
    coder: str
    postgres: str


class RemoteCoderStack:
    def __init__(self, images: RuntimeImages, socket_group_id: int | None = None) -> None:
        prefix = f"task4-{uuid4().hex[:20]}"
        self._images = images
        self._network = f"{prefix}-network"
        self._postgres = f"{prefix}-postgres"
        self._coder = f"{prefix}-coder"
        self._password = secrets.token_urlsafe(24)
        self._created_containers: list[str] = []
        self._network_created = False
        self._workspace_container: str | None = None
        self._socket_group_id = (
            read_socket_group_id() if socket_group_id is None else socket_group_id
        )
        if self._socket_group_id < 0:
            raise RemoteCoderProtocolError("remote Docker socket group is invalid")

    @property
    def coder_container(self) -> str:
        return self._coder

    @property
    def template_address(self) -> WorkspaceTemplateAddress:
        return WorkspaceTemplateAddress(self._network, f"{self._coder}-workspace")

    def register_workspace_container(self, workspace_id: str) -> None:
        try:
            normalized_id = str(UUID(workspace_id))
        except ValueError as error:
            raise RemoteCoderProtocolError("remote workspace identifier is invalid") from error
        name = f"{self.template_address.workspace_prefix}-{normalized_id}"
        if name in self._created_containers:
            raise RemoteCoderProtocolError("remote workspace container was registered twice")
        self._created_containers.append(name)
        self._workspace_container = name

    def probe_workspace_runtime(self) -> str:
        if self._workspace_container is None:
            return "workspace-container-missing"
        return WorkspaceRuntimeProbe(
            self._workspace_container, self._network, self._coder
        )()

    def start(self) -> RemoteCoderEndpoint:
        self._create_network()
        self._start_postgres()
        self._wait_for_postgres()
        self._start_coder()
        return RemoteCoderEndpoint(port=self._published_port())

    def wait_for_provisioners(self, session_token: str) -> None:
        stable_observations = 0
        for _ in range(_PROVISIONER_READY_ATTEMPTS):
            self._require(
                self._coder_running(), "Coder container running state"
            )
            if self._provisioner_daemon_count(session_token) == _BUILTIN_PROVISIONER_DAEMONS:
                stable_observations += 1
                if stable_observations == _PROVISIONER_STABLE_OBSERVATIONS:
                    return
            else:
                stable_observations = 0
            time.sleep(0.25)
        raise RemoteCoderProtocolError("remote Coder provisioner inventory did not stabilize")

    def cleanup(self) -> bool:
        completed = True
        for container in reversed(self._created_containers):
            completed = self._command(("docker", "rm", "--force", container)) and completed
        if self._network_created:
            completed = self._command(("docker", "network", "rm", self._network)) and completed
        return completed

    def _create_network(self) -> None:
        self._require(self._command(("docker", "network", "create", self._network)), "network")
        self._network_created = True

    def _start_postgres(self) -> None:
        self._require(
            self._command(
                (
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    self._postgres,
                    "--network",
                    self._network,
                    "--env",
                    "POSTGRES_USER=coder",
                    "--env",
                    f"POSTGRES_PASSWORD={self._password}",
                    "--env",
                    "POSTGRES_DB=coder",
                    self._images.postgres,
                )
            ),
            "PostgreSQL container",
        )
        self._created_containers.append(self._postgres)

    def _wait_for_postgres(self) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._command(
                (
                    "docker",
                    "exec",
                    self._postgres,
                    "pg_isready",
                    "--username",
                    "coder",
                    "--dbname",
                    "coder",
                )
            ):
                return
            time.sleep(0.25)
        raise RemoteCoderProtocolError("remote PostgreSQL readiness deadline expired")

    def _start_coder(self) -> None:
        self._require(
            self._command(
                (
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    self._coder,
                    "--network",
                    self._network,
                    "--publish",
                    "127.0.0.1::3000",
                    "--volume",
                    "/var/run/docker.sock:/var/run/docker.sock",
                    "--group-add",
                    str(self._socket_group_id),
                    "--env",
                    "CODER_HTTP_ADDRESS=0.0.0.0:3000",
                    "--env",
                    f"CODER_ACCESS_URL=http://{self._coder}:3000",
                    "--env",
                    f"CODER_PROVISIONER_DAEMONS={_BUILTIN_PROVISIONER_DAEMONS}",
                    "--env",
                    (
                        "CODER_PG_CONNECTION_URL="
                        f"postgres://coder:{self._password}@{self._postgres}:5432/coder?sslmode=disable"
                    ),
                    self._images.coder,
                )
            ),
            "Coder container",
        )
        self._created_containers.append(self._coder)

    def _published_port(self) -> int:
        result = subprocess.run(
            ("docker", "port", self._coder, "3000/tcp"),
            check=False,
            capture_output=True,
        )
        match result.returncode, result.stdout:
            case 0, bytes() as output:
                return parse_loopback_port(output)
            case _:
                raise RemoteCoderProtocolError("remote Coder published port lookup failed")

    def _provisioner_daemon_count(self, session_token: str) -> int:
        result = subprocess.run(
            (
                "docker",
                "exec",
                "--env",
                "CODER_URL=http://localhost:3000",
                "--env",
                f"CODER_SESSION_TOKEN={session_token}",
                self._coder,
                "/opt/coder",
                "provisioner",
                "list",
                "--output",
                "json",
            ),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RemoteCoderProtocolError(
                "remote Coder provisioner inventory command failed"
            )
        try:
            value: JsonValue = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteCoderProtocolError(
                "remote Coder provisioner inventory is not JSON"
            ) from error
        match value:
            case list() as daemons:
                return len(daemons)
            case _:
                raise RemoteCoderProtocolError("remote Coder provisioner inventory is not an array")

    def _coder_running(self) -> bool:
        result = subprocess.run(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                self._coder,
            ),
            check=False,
            capture_output=True,
        )
        return result.returncode == 0 and result.stdout.strip() == b"true"

    def _command(self, arguments: tuple[str, ...]) -> bool:
        return subprocess.run(arguments, check=False, capture_output=True).returncode == 0

    def _require(self, succeeded: bool, label: str) -> None:
        if not succeeded:
            raise RemoteCoderProtocolError(f"remote {label} command failed")
__all__ = ["RemoteCoderStack", "RuntimeImages"]
