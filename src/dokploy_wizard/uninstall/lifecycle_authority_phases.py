"""Phase-result adapters for publishing create-time uninstall authority."""

from __future__ import annotations

from typing import Protocol

from dokploy_wizard.packs.headscale import HEADSCALE_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.headscale.models import HeadscalePhase
from dokploy_wizard.packs.matrix import (
    MATRIX_DATA_RESOURCE_TYPE,
    MATRIX_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.matrix.models import MatrixPhase
from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.openclaw.models import OpenClawPhase
from dokploy_wizard.packs.seaweedfs import (
    SEAWEEDFS_DATA_RESOURCE_TYPE,
    SEAWEEDFS_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.seaweedfs.models import SeaweedFsPhase
from dokploy_wizard.state import OwnedResource, OwnershipLedger
from dokploy_wizard.state.uninstall_authority_schema import UninstallAuthorityError
from dokploy_wizard.tailscale.models import TailscalePhase
from dokploy_wizard.uninstall.lifecycle_authority import (
    LifecycleAuthorityPublisher,
    TailscaleAuthorityReader,
)


class ManagedResource(Protocol):
    """Lifecycle result fragment exposing its creation disposition."""

    @property
    def action(self) -> str: ...

    @property
    def resource_name(self) -> str: ...


def publish_tailscale_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: TailscalePhase,
    reader: TailscaleAuthorityReader,
) -> None:
    """Publish authority only when Tailscale reconciliation created a node."""

    node = phase.result.node
    if publisher is None or node is None or phase.node_resource_id is None:
        return
    if node.action != "create":
        return
    resource = _resource(ledger, "tailscale_node", phase.node_resource_id)
    publisher.record_created_tailscale(resource, node, reader)


def publish_headscale_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: HeadscalePhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        HEADSCALE_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )


def publish_matrix_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: MatrixPhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        MATRIX_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )
    _publish_volume(
        publisher,
        ledger,
        MATRIX_DATA_RESOURCE_TYPE,
        phase.result.persistent_data,
        phase.data_resource_id,
    )


def publish_seaweedfs_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: SeaweedFsPhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        SEAWEEDFS_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )
    _publish_volume(
        publisher,
        ledger,
        SEAWEEDFS_DATA_RESOURCE_TYPE,
        phase.result.persistent_data,
        phase.data_resource_id,
    )




def publish_openclaw_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: OpenClawPhase,
    *,
    include_sidecars: bool,
) -> None:
    if publisher is None:
        return
    resource_type = (
        OPENCLAW_SERVICE_RESOURCE_TYPE
        if include_sidecars
        else MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE
    )
    _publish_service(
        publisher,
        ledger,
        resource_type,
        phase.result.service,
        phase.service_resource_id,
    )
    if publisher is None or phase.service_resource_id is None:
        return
    if not _was_created(phase.result.service):
        return
    for sidecar_type in (
        OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
        OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
        OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    ):
        publisher.record_created_compose(
            _sole_resource_of_type(ledger, sidecar_type), phase.service_resource_id
        )


def _publish_service(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    resource_type: str,
    managed: ManagedResource | None,
    resource_id: str | None,
) -> None:
    if publisher is None or managed is None or resource_id is None:
        return
    if not _was_created(managed):
        return
    publisher.record_created_compose(_resource(ledger, resource_type, resource_id), resource_id)


def _publish_volume(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    resource_type: str,
    managed: ManagedResource | None,
    resource_id: str | None,
) -> None:
    if publisher is None or managed is None or resource_id is None:
        return
    if not _was_created(managed):
        return
    publisher.record_created_volume(
        _resource(ledger, resource_type, resource_id), managed.resource_name
    )


def _was_created(resource: ManagedResource | None) -> bool:
    return resource is not None and resource.action == "create"


def _resource(ledger: OwnershipLedger, resource_type: str, resource_id: str) -> OwnedResource:
    matches = tuple(
        resource
        for resource in ledger.resources
        if resource.resource_type == resource_type and resource.resource_id == resource_id
    )
    if len(matches) != 1:
        raise UninstallAuthorityError(
            "Lifecycle ledger resource is absent or ambiguous for authority."
        )
    return matches[0]


def _sole_resource_of_type(ledger: OwnershipLedger, resource_type: str) -> OwnedResource:
    matches = tuple(
        resource for resource in ledger.resources if resource.resource_type == resource_type
    )
    if len(matches) != 1:
        raise UninstallAuthorityError("Lifecycle sidecar ledger resource is absent or ambiguous.")
    return matches[0]
