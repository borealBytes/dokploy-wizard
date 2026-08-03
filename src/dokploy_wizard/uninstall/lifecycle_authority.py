"""Create-time authority publication for receipt-bound lifecycle resources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dokploy_wizard.dokploy.client import DokployApiClient, DokployProjectSummary
from dokploy_wizard.state.models import OwnedResource, RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_authority_schema import (
    UninstallAuthority,
    UninstallAuthorityError,
)
from dokploy_wizard.state.uninstall_targets import (
    DockerVolumeRecord,
    DokployComposeDeletionRecord,
    docker_volume_fingerprint,
    dokploy_compose_fingerprint,
)
from dokploy_wizard.tailscale import TailscaleManagedResource
from dokploy_wizard.uninstall.docker_client import SubprocessDockerDeletionClient


class DokployAuthorityReader(Protocol):
    """Provider read capability required before publishing compose deletion authority."""

    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...


class DockerVolumeAuthorityReader(Protocol):
    """Provider read capability required before publishing volume deletion authority."""

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None: ...


class TailscaleAuthorityReader(Protocol):
    """Provider read capability required before publishing node deletion authority."""

    def get_node(self, resource_id: str) -> TailscaleManagedResource | None: ...


@dataclass(frozen=True, slots=True)
class LifecycleAuthorityPublisher:
    """Publishes authority only after exact post-create provider rereads."""

    store: UninstallAuthorityStore
    dokploy: DokployAuthorityReader
    docker: DockerVolumeAuthorityReader
    stack_name: str

    def record_created_compose(
        self, resource: OwnedResource, compose_resource_id: str
    ) -> UninstallAuthority:
        """Bind a newly-created logical resource to its re-read Dokploy compose target."""

        compose_id = _compose_id_from_resource_id(compose_resource_id)
        project, compose = _find_compose(self.dokploy.list_projects(), compose_id)
        target = DokployComposeDeletionRecord(
            compose_id=compose.compose_id,
            project_id=project.project_id,
            name=compose.name,
        )
        return self.store.record_created(
            resource=resource,
            owner_id=f"stack:{self.stack_name}",
            provider="dokploy_compose",
            physical_target_id=target.compose_id,
            parent_target_id=target.project_id,
            expected_fingerprint=dokploy_compose_fingerprint(target),
        )

    def record_created_volume(
        self, resource: OwnedResource, volume_name: str
    ) -> UninstallAuthority:
        """Bind a newly-created logical resource to its re-read Docker volume target."""

        volume = self.docker.get_volume(volume_name)
        if volume is None:
            raise UninstallAuthorityError(
                "Created Docker volume was absent during authority reread."
            )
        if volume.name != volume_name:
            raise UninstallAuthorityError(
                "Created Docker volume name changed before authority publication."
            )
        return self.store.record_created(
            resource=resource,
            owner_id=f"stack:{self.stack_name}",
            provider="docker_volume",
            physical_target_id=volume.volume_id,
            parent_target_id="docker",
            expected_fingerprint=docker_volume_fingerprint(volume),
        )

    def record_created_tailscale(
        self,
        resource: OwnedResource,
        expected: TailscaleManagedResource,
        reader: TailscaleAuthorityReader,
    ) -> UninstallAuthority:
        """Bind a newly-created Tailscale node to its exact post-create reread."""

        node = reader.get_node(expected.resource_id)
        if node is None:
            raise UninstallAuthorityError(
                "Created Tailscale node was absent during authority reread."
            )
        if node != expected:
            raise UninstallAuthorityError(
                "Created Tailscale node changed before authority publication."
            )
        from dokploy_wizard.uninstall.providers import tailscale_fingerprint

        return self.store.record_created(
            resource=resource,
            owner_id=f"stack:{self.stack_name}",
            provider="tailscale",
            physical_target_id=node.resource_id,
            parent_target_id=resource.scope,
            expected_fingerprint=tailscale_fingerprint(node),
        )


def production_lifecycle_authority_publisher(
    raw_env: RawEnvInput, state_dir: Path, stack_name: str
) -> LifecycleAuthorityPublisher:
    """Construct the live readers required to publish newly-created authority."""

    api_url = raw_env.values.get("DOKPLOY_API_URL", "")
    api_key = raw_env.values.get("DOKPLOY_API_KEY", "")
    if api_url == "" or api_key == "":
        raise UninstallAuthorityError(
            "Dokploy authority publication requires runtime API credentials."
        )
    return LifecycleAuthorityPublisher(
        store=UninstallAuthorityStore(state_dir),
        dokploy=DokployApiClient(api_url=api_url, api_key=api_key),
        docker=SubprocessDockerDeletionClient(),
        stack_name=stack_name,
    )


def _compose_id_from_resource_id(resource_id: str) -> str:
    prefix = "dokploy-compose:"
    if not resource_id.startswith(prefix):
        raise UninstallAuthorityError("Created resource does not expose a Dokploy compose target.")
    compose_id, separator, _ = resource_id[len(prefix) :].partition(":")
    if compose_id == "" or separator == "":
        raise UninstallAuthorityError("Created resource has an invalid Dokploy compose target.")
    return compose_id


def _find_compose(
    projects: tuple[DokployProjectSummary, ...], compose_id: str
) -> tuple[DokployProjectSummary, DokployComposeDeletionRecord]:
    matches = tuple(
        (project, compose)
        for project in projects
        for environment in project.environments
        for compose in environment.composes
        if compose.compose_id == compose_id
    )
    if len(matches) == 0:
        raise UninstallAuthorityError("Created Dokploy compose was absent during authority reread.")
    if len(matches) != 1:
        raise UninstallAuthorityError("Created Dokploy compose reread is ambiguous.")
    project, compose = matches[0]
    return project, DokployComposeDeletionRecord(
        compose_id=compose.compose_id,
        project_id=project.project_id,
        name=compose.name,
    )
