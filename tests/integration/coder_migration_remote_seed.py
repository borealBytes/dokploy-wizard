from __future__ import annotations

import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, assert_never

from dokploy_wizard.dokploy.coder_migration_api import CoderMigrationApi, UrllibCoderTransport
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderProtocolError,
    CoderTemplate,
)
from tests.integration.coder_migration_remote_http import (
    ReadinessTiming,
    RemoteCoderEndpoint,
    RemoteCoderRequest,
    request_coder,
    wait_for_coder_readiness,
)
from tests.integration.coder_migration_remote_protocol import (
    FirstUserMissing,
    FirstUserPresent,
    RemoteCoderProtocolError,
    RemoteCoderResponse,
    parse_created_build,
    parse_created_user,
    parse_created_workspace,
    parse_default_organization,
    parse_login,
)
from tests.integration.coder_migration_remote_template import (
    WorkspaceTemplateAddress,
    render_template,
    template_push_command,
)
from tests.integration.coder_migration_remote_workspace import wait_for_workspace_status


@dataclass(frozen=True, slots=True)
class CoderRuntimeIdentifiers:
    email: str
    username: str
    template_name: str
    workspace_name: str


@dataclass(frozen=True, slots=True)
class SeededCoderWorkspace:
    api: CoderMigrationApi
    workspace_id: str
    seeder: RemoteCoderSeeder


class RemoteCoderSeeder:
    def __init__(
        self,
        endpoint: RemoteCoderEndpoint,
        coder_container: str,
        wait_for_provisioners: Callable[[str], None],
        template_address: WorkspaceTemplateAddress,
        register_workspace_container: Callable[[str], None],
        probe_workspace_runtime: Callable[[], str],
    ) -> None:
        self._endpoint = endpoint
        self._coder_container = coder_container
        self._wait_for_provisioners = wait_for_provisioners
        self._template_address = template_address
        self._register_workspace_container = register_workspace_container
        self._probe_workspace_runtime = probe_workspace_runtime
        self._identifiers = _identifiers()
        self._password = secrets.token_urlsafe(24)
        self._session_token: str | None = None
        self._api: CoderMigrationApi | None = None

        self._phase = "readiness"

    @property
    def phase(self) -> str:
        return self._phase

    def seed_workspace(self) -> SeededCoderWorkspace:
        self._phase = "readiness"
        self._bootstrap()
        self._phase = "provisioner"
        self._wait_for_provisioners(self._require_token())
        api = self._require_api()
        self._phase = "organization"
        organization_id = parse_default_organization(
            self._read("/api/v2/organizations/default")
        ).id
        self._phase = "template"
        self._push_template()
        template = _require_template(api, str(organization_id), self._identifiers.template_name)
        self._phase = "workspace-create"
        workspace = parse_created_workspace(
            self._write(
                "/api/v2/users/me/workspaces",
                {
                    "name": self._identifiers.workspace_name,
                    "template_id": str(template.id),
                },
            )
        )
        workspace_id = str(workspace.id)
        api = self._require_api()
        self._register_workspace_container(workspace_id)
        self._phase = "workspace-running"
        wait_for_workspace_status(
            api, self._read, workspace_id, "running", self._probe_workspace_runtime
        )
        self._phase = "workspace-stop"
        self.submit_build(workspace_id, "stop")
        self._phase = "workspace-stopped"
        wait_for_workspace_status(
            api, self._read, workspace_id, "stopped", self._probe_workspace_runtime
        )
        return SeededCoderWorkspace(api=api, workspace_id=workspace_id, seeder=self)

    def submit_build(self, workspace_id: str, transition: str) -> CoderBuild:
        self._phase = f"workspace-{transition}"
        response = self._write(
            f"/api/v2/workspaces/{workspace_id}/builds", {"transition": transition}
        )
        return parse_created_build(response, f"Coder {transition} build")

    def _bootstrap(self) -> None:
        first_user = wait_for_coder_readiness(
            self._read_unauthenticated,
            ReadinessTiming(120, time.monotonic, time.sleep),
        )
        match first_user:
            case FirstUserMissing():
                parse_created_user(
                    self._write_unauthenticated(
                        "/api/v2/users/first",
                        {
                            "email": self._identifiers.email,
                            "name": self._identifiers.username,
                            "password": self._password,
                            "username": self._identifiers.username,
                        },
                    )
                )
            case FirstUserPresent():
                raise RemoteCoderProtocolError(
                    "isolated remote Coder unexpectedly has a first user"
                )
            case unreachable:
                assert_never(unreachable)
        login = parse_login(
            self._write_unauthenticated(
                "/api/v2/users/login",
                {"email": self._identifiers.email, "password": self._password},
            )
        )
        self._session_token = login.session_token
        self._api = CoderMigrationApi(
            transport=UrllibCoderTransport(f"http://127.0.0.1:{self._endpoint.port}"),
            session_token=login.session_token,
        )

    def _push_template(self) -> None:
        with TemporaryDirectory(prefix="task4-coder-template-") as directory:
            template_dir = Path(directory)
            (template_dir / "main.tf").write_text(
                render_template(self._template_address), encoding="utf-8"
            )
            self._command(
                ("docker", "exec", self._coder_container, "mkdir", "--parents", "/tmp/template"),
                "template-directory",
            )
            self._command(
                (
                    "docker",
                    "cp",
                    f"{template_dir}/.",
                    f"{self._coder_container}:/tmp/template",
                ),
                "template-copy",
            )
            self._command(
                template_push_command(
                    self._coder_container,
                    self._identifiers.template_name,
                    self._require_token(),
                ),
                "template-push",
            )

    def _read_unauthenticated(self, path: str) -> RemoteCoderResponse:
        return request_coder(
            self._endpoint, RemoteCoderRequest(method="GET", path=path, payload=None)
        )

    def _write_unauthenticated(
        self, path: str, payload: dict[str, str]
    ) -> RemoteCoderResponse:
        return request_coder(
            self._endpoint, RemoteCoderRequest(method="POST", path=path, payload=payload)
        )

    def _read(self, path: str) -> RemoteCoderResponse:
        return request_coder(
            self._endpoint,
            RemoteCoderRequest(
                method="GET", path=path, payload=None, headers=self._session_headers()
            ),
        )

    def _write(self, path: str, payload: dict[str, str]) -> RemoteCoderResponse:
        return request_coder(
            self._endpoint,
            RemoteCoderRequest(
                method="POST", path=path, payload=payload, headers=self._session_headers()
            ),
        )

    def _session_headers(self) -> tuple[tuple[str, str], ...]:
        return (("Coder-Session-Token", self._require_token()),)

    def _require_token(self) -> str:
        if self._session_token is None:
            raise RemoteCoderProtocolError("remote Coder session token is unavailable")
        return self._session_token

    def _require_api(self) -> CoderMigrationApi:
        if self._api is None:
            raise RemoteCoderProtocolError("remote Coder migration API is unavailable")
        return self._api

    def _command(self, arguments: tuple[str, ...], label: str) -> None:
        result = subprocess.run(arguments, check=False, capture_output=True)
        if result.returncode != 0:
            cause = _command_cause(result.stdout + result.stderr)
            raise RemoteCoderProtocolError(
                f"remote Coder {label}-{cause} command failed"
            )


def _identifiers() -> CoderRuntimeIdentifiers:
    suffix = secrets.token_hex(10)
    username = f"u{suffix}"
    return CoderRuntimeIdentifiers(
        email=f"{username}@example.invalid",
        username=username,
        template_name=f"t{suffix}",
        workspace_name=f"w{suffix}",
    )


def _require_template(api: CoderMigrationApi, organization_id: str, name: str) -> CoderTemplate:
    templates = tuple(
        template for template in api.list_templates(organization_id) if template.name == name
    )
    if len(templates) != 1:
        raise CoderProtocolError("remote Coder template inventory is not exact")
    return templates[0]


def _command_cause(stderr: bytes) -> str:
    lowered = stderr.lower()
    if b"invalid single-argument block definition" in lowered:
        return "hcl"
    if b"terraform" in lowered:
        return "terraform"
    if b"provisioner" in lowered:
        return "provisioner"
    if b"authorized" in lowered:
        return "authorization"
    return "other"


__all__ = ["RemoteCoderSeeder", "SeededCoderWorkspace"]
