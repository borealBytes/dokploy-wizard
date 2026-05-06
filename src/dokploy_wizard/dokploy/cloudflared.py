"""Dokploy-backed Cloudflare Tunnel connector backend."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib import error, request

from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import apply_compose_noop_guard
from dokploy_wizard.verification import make_verification_result


class CloudflaredConnectorError(RuntimeError):
    """Raised when the managed Cloudflare connector cannot be reconciled."""


@dataclass(frozen=True)
class CloudflaredConnectorRecord:
    resource_id: str
    resource_name: str


class DokployCloudflaredApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokployCloudflaredBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        state_dir: Path,
        stack_name: str,
        public_url: str,
        client: DokployCloudflaredApi | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._stack_name = stack_name
        self._public_url = public_url
        self._service_name = f"{stack_name}-cloudflared"
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None

    def get_service(self, resource_id: str) -> CloudflaredConnectorRecord | None:
        compose_id = _parse_resource_id(resource_id)
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CloudflaredConnectorRecord(resource_id=resource_id, resource_name=self._service_name)

    def find_service_by_name(self, resource_name: str) -> CloudflaredConnectorRecord | None:
        if resource_name != self._service_name:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CloudflaredConnectorRecord(
            resource_id=_resource_id(locator.compose_id),
            resource_name=self._service_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        tunnel_token: str,
    ) -> CloudflaredConnectorRecord:
        if resource_name != self._service_name:
            raise CloudflaredConnectorError(
                "Cloudflare connector service name does not match the active Dokploy plan."
            )
        locator = self._ensure_compose_applied(tunnel_token=tunnel_token)
        return CloudflaredConnectorRecord(
            resource_id=_resource_id(locator.compose_id),
            resource_name=self._service_name,
        )

    def check_health(self, *, service: CloudflaredConnectorRecord, url: str) -> bool:
        del service
        return _wait_for_public_url(url)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error_value:
            raise CloudflaredConnectorError(str(error_value)) from error_value
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._service_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self, *, tunnel_token: str) -> _ComposeLocator:
        compose_file = _render_compose_file(self._service_name, tunnel_token=tunnel_token)
        try:
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._service_name:
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=compose.compose_id,
                        )
                        applied = apply_compose_noop_guard(
                            rendered_compose=compose_file,
                            service_key=self._service_name,
                            state_dir=self._state_dir,
                            client=self._client,
                            locator=locator,
                            compose_id=compose.compose_id,
                            title="dokploy-wizard cloudflared reconcile",
                            description="Update Cloudflare Tunnel connector compose app",
                            verify_current=self._verify_current_service,
                            locator_factory=lambda compose_id: _ComposeLocator(
                                project_id=project.project_id,
                                environment_id=environment.environment_id,
                                compose_id=compose_id,
                            ),
                        )
                        self._applied_locator = applied.locator
                        return applied.locator

                created = self._client.create_compose(
                    name=self._service_name,
                    environment_id=environment.environment_id,
                    compose_file=compose_file,
                    app_name=self._service_name,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard cloudflared reconcile",
                    description="Create Cloudflare Tunnel connector compose app",
                )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._applied_locator = locator
                return locator

            created_project = self._client.create_project(
                name=self._stack_name,
                description="Managed by dokploy-wizard",
                env=None,
            )
            created = self._client.create_compose(
                name=self._service_name,
                environment_id=created_project.environment_id,
                compose_file=compose_file,
                app_name=self._service_name,
            )
            self._client.deploy_compose(
                compose_id=created.compose_id,
                title="dokploy-wizard cloudflared reconcile",
                description="Create Cloudflare Tunnel connector compose app",
            )
            locator = _ComposeLocator(
                project_id=created_project.project_id,
                environment_id=created_project.environment_id,
                compose_id=created.compose_id,
            )
            self._applied_locator = locator
            return locator
        except DokployApiError as error_value:
            raise CloudflaredConnectorError(str(error_value)) from error_value

    def _verify_current_service(self):
        is_up = _docker_container_is_up(self._service_name)
        return make_verification_result(
            service_name=self._service_name,
            tier="app",
            passed=is_up,
            detail=(
                f"Cloudflared container for '{self._service_name}' is "
                f"{'up' if is_up else 'not up'}."
            ),
            evidence_command=["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        )


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _resource_id(compose_id: str) -> str:
    return f"dokploy-compose:{compose_id}:cloudflared"


def _parse_resource_id(resource_id: str) -> str | None:
    prefix = "dokploy-compose:"
    if not resource_id.startswith(prefix):
        return None
    suffix = resource_id.removeprefix(prefix)
    compose_id, _, kind = suffix.partition(":")
    if not compose_id or kind != "cloudflared":
        return None
    return compose_id


def _render_compose_file(service_name: str, *, tunnel_token: str) -> str:
    encoded_token = json.dumps(tunnel_token)
    return (
        "services:\n"
        f"  {service_name}:\n"
        "    image: cloudflare/cloudflared:latest\n"
        "    restart: unless-stopped\n"
        "    network_mode: host\n"
        "    command: ['tunnel', '--no-autoupdate', 'run']\n"
        "    environment:\n"
        f"      TUNNEL_TOKEN: {encoded_token}\n"
    )


def _docker_container_is_up(service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if service_name not in name:
            continue
        return status.startswith("Up ")
    return False


def _wait_for_public_url(url: str, *, attempts: int = 24, delay_seconds: float = 5.0) -> bool:
    for _ in range(attempts):
        if _public_url_ready(url):
            return True
        time.sleep(delay_seconds)
    return False


def _public_url_ready(url: str) -> bool:
    req = request.Request(url, method="GET", headers={"Accept": "text/html,application/json"})
    try:
        with request.urlopen(req, timeout=15) as response:  # noqa: S310
            status = cast(int, response.status)
            return 200 <= status < 500
    except error.HTTPError as exc:
        return exc.code < 500 and exc.code != 530
    except (error.URLError, TimeoutError):
        return False
