from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.state import OwnedResource, parse_env_file
from dokploy_wizard.state.shared_core_sync import SyncOwnershipMetadata
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_provenance import (
    ProviderCreationDisposition,
    ProviderCreationResult,
    publish_created_authority,
)
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.executor import ShellUninstallBackend
from dokploy_wizard.uninstall.families import ProviderFamily, family_for
from dokploy_wizard.uninstall.planner import _RULES, PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    DockerNetworkRecord,
    DockerVolumeRecord,
    DokployComposeDeletionRecord,
    UninstallProviderClients,
    docker_network_fingerprint,
    docker_volume_fingerprint,
    dokploy_compose_fingerprint,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


class DokployClient:
    def __init__(self) -> None:
        self.compose: DokployComposeDeletionRecord | None = DokployComposeDeletionRecord(
            compose_id="compose-1", project_id="project-1", name="wizard-nextcloud"
        )
        self.calls: list[str] = []

    def get_compose(
        self, project_id: str, compose_id: str
    ) -> DokployComposeDeletionRecord | None:
        assert (project_id, compose_id) == ("project-1", "compose-1")
        self.calls.append("get_compose")
        return self.compose

    def delete_compose(self, compose_id: str) -> None:
        assert compose_id == "compose-1"
        self.calls.append("delete_compose")
        self.compose = None


class DockerClient:
    def __init__(self) -> None:
        self.volume: DockerVolumeRecord | None = DockerVolumeRecord("volume-1", "wizard-data")
        self.network: DockerNetworkRecord | None = DockerNetworkRecord("network-1", "wizard-net")
        self.calls: list[str] = []

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None:
        assert volume_id == "volume-1"
        self.calls.append("get_volume")
        return self.volume

    def delete_volume(self, volume_id: str) -> None:
        assert volume_id == "volume-1"
        self.calls.append("delete_volume")
        self.volume = None

    def get_network(self, network_id: str) -> DockerNetworkRecord | None:
        assert network_id == "network-1"
        self.calls.append("get_network")
        return self.network

    def delete_network(self, network_id: str) -> None:
        assert network_id == "network-1"
        self.calls.append("delete_network")
        self.network = None


class DockerInspectFailureAfterDeleteClient(DockerClient):
    def __init__(self) -> None:
        super().__init__()
        self._deleted = False

    def delete_volume(self, volume_id: str) -> None:
        super().delete_volume(volume_id)
        self._deleted = True

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None:
        if self._deleted:
            raise UninstallExecutionError("Docker inspection failed.")
        return super().get_volume(volume_id)


def _deletion(resource: OwnedResource) -> PlannedDeletion:
    return PlannedDeletion(resource=resource, phase="nextcloud", policy="destroy_only")


def _resource_for_provenance(resource_type: str, index: int) -> OwnedResource:
    resource_id = f"logical-{index}"
    if resource_type != "shared_core_sync_schedule":
        return OwnedResource(resource_type, resource_id, f"stack:wizard:{index}")
    return OwnedResource(
        resource_type,
        resource_id,
        f"stack:wizard:{index}",
        metadata=SyncOwnershipMetadata(
            owner_id=_OWNER,
            action_provenance="created",
            remote_fingerprint="a" * 64,
            spec_hash="b" * 64,
            physical_target_id=resource_id,
            creation_receipt_sha256="c" * 64,
            preimage_receipt_sha256=None,
            deletion_policy="delete",
        ),
    )


def test_every_planner_resource_type_has_one_provider_family() -> None:
    families = {family_for(resource_type) for resource_type in _RULES}

    assert families == {
        ProviderFamily.SCHEDULE,
        ProviderFamily.CLOUDFLARE,
        ProviderFamily.TAILSCALE,
        ProviderFamily.DOKPLOY,
        ProviderFamily.DOCKER,
    }


def test_default_backend_coalesces_receipt_bound_dokploy_compose_deletion(tmp_path: Path) -> None:
    first = OwnedResource("nextcloud_service", "logical-nextcloud", "stack:wizard:nextcloud")
    second = OwnedResource("onlyoffice_service", "logical-onlyoffice", "stack:wizard:onlyoffice")
    authorities = UninstallAuthorityStore(tmp_path)
    compose = DokployComposeDeletionRecord("compose-1", "project-1", "wizard-nextcloud")
    for resource in (first, second):
        authorities.record_created(
            resource=resource,
            owner_id=_OWNER,
            provider="dokploy_compose",
            physical_target_id=compose.compose_id,
            parent_target_id=compose.project_id,
            expected_fingerprint=dokploy_compose_fingerprint(compose),
        )
    client = DokployClient()
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(dokploy=client),
    )

    backend.delete(_deletion(first))
    backend.delete(_deletion(second))

    assert client.calls == ["get_compose", "delete_compose", "get_compose", "get_compose"]
    assert authorities.load_deletion(first) is not None
    assert authorities.load_deletion(second) is not None


def test_default_backend_deletes_exact_receipt_bound_docker_targets(tmp_path: Path) -> None:
    volume_resource = OwnedResource("nextcloud_volume", "logical-volume", "stack:wizard:volume")
    network_resource = OwnedResource(
        "shared_core_network", "logical-network", "stack:wizard:network"
    )
    authorities = UninstallAuthorityStore(tmp_path)
    volume = DockerVolumeRecord("volume-1", "wizard-data")
    network = DockerNetworkRecord("network-1", "wizard-net")
    authorities.record_created(
        resource=volume_resource,
        owner_id=_OWNER,
        provider="docker_volume",
        physical_target_id=volume.volume_id,
        parent_target_id="docker",
        expected_fingerprint=docker_volume_fingerprint(volume),
    )
    authorities.record_created(
        resource=network_resource,
        owner_id=_OWNER,
        provider="docker_network",
        physical_target_id=network.network_id,
        parent_target_id="docker",
        expected_fingerprint=docker_network_fingerprint(network),
    )
    client = DockerClient()
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(docker=client),
    )

    backend.delete(_deletion(volume_resource))
    backend.delete(_deletion(network_resource))

    assert client.calls == [
        "get_volume",
        "delete_volume",
        "get_volume",
        "get_network",
        "delete_network",
        "get_network",
    ]


def test_docker_inspection_failure_after_delete_does_not_write_receipt(tmp_path: Path) -> None:
    resource = OwnedResource("nextcloud_volume", "logical-volume", "stack:wizard:volume")
    volume = DockerVolumeRecord("volume-1", "wizard-data")
    authorities = UninstallAuthorityStore(tmp_path)
    authorities.record_created(
        resource=resource,
        owner_id=_OWNER,
        provider="docker_volume",
        physical_target_id=volume.volume_id,
        parent_target_id="docker",
        expected_fingerprint=docker_volume_fingerprint(volume),
    )
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(docker=DockerInspectFailureAfterDeleteClient()),
    )

    with pytest.raises(UninstallExecutionError, match="inspection failed"):
        backend.delete(_deletion(resource))

    assert authorities.load_deletion(resource) is None


def test_default_backend_rejects_legacy_dokploy_ledger_without_authority(tmp_path: Path) -> None:
    resource = OwnedResource("nextcloud_service", "legacy-nextcloud", "stack:wizard:nextcloud")
    client = DokployClient()
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(dokploy=client),
    )

    with pytest.raises(UninstallExecutionError, match="recorded uninstall authority"):
        backend.delete(_deletion(resource))

    assert client.calls == []


def test_default_backend_rejects_legacy_docker_ledger_without_authority(tmp_path: Path) -> None:
    resource = OwnedResource("nextcloud_volume", "legacy-nextcloud-data", "stack:wizard:data")
    client = DockerClient()
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(docker=client),
    )

    with pytest.raises(UninstallExecutionError, match="recorded uninstall authority"):
        backend.delete(_deletion(resource))

    assert client.calls == []


def test_default_backend_simulated_failure_precedes_provider_mutation(tmp_path: Path) -> None:
    resource = OwnedResource("nextcloud_volume", "logical-volume", "stack:wizard:volume")
    client = DockerClient()
    backend = ShellUninstallBackend(
        parse_env_file(_FIXTURES / "nextcloud.env"),
        state_dir=tmp_path,
        providers=UninstallProviderClients(docker=client),
    )
    backend._failing_types.add(resource.resource_type)

    with pytest.raises(UninstallExecutionError, match="Simulated uninstall failure"):
        backend.delete(_deletion(resource))

    assert client.calls == []


@pytest.mark.parametrize(
    "disposition",
    (
        ProviderCreationDisposition.CREATED,
        ProviderCreationDisposition.REUSED,
        ProviderCreationDisposition.UPDATED,
    ),
)
def test_provenance_migration_all_resource_types_publishes_only_exact_creations(
    tmp_path: Path,
    disposition: ProviderCreationDisposition,
) -> None:
    resources = tuple(
        _resource_for_provenance(resource_type, index)
        for index, resource_type in enumerate(sorted(_RULES))
    )
    store = UninstallAuthorityStore(tmp_path)

    for resource in resources:
        publish_created_authority(
            store,
            ProviderCreationResult(
                disposition=disposition,
                resource=resource,
                owner_id=_OWNER,
                provider="dokploy_compose",
                physical_target_id=f"compose-{resource.resource_id}",
                parent_target_id="project-1",
                expected_fingerprint="a" * 64,
            ),
        )

    expected = len(resources) if disposition is ProviderCreationDisposition.CREATED else 0
    assert sum(store.load_created(resource) is not None for resource in resources) == expected
