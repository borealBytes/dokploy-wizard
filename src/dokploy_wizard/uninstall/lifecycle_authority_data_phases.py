"""Data-bearing pack phase adapters for create-time uninstall authority."""

from __future__ import annotations

from dokploy_wizard.packs.coder import (
    CODER_DATA_RESOURCE_TYPE,
    CODER_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.coder.models import CoderPhase
from dokploy_wizard.packs.docuseal import (
    DOCUSEAL_DATA_RESOURCE_TYPE,
    DOCUSEAL_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.docuseal.models import DocuSealPhase
from dokploy_wizard.packs.moodle import (
    MOODLE_DATA_RESOURCE_TYPE,
    MOODLE_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.moodle.models import MoodlePhase
from dokploy_wizard.packs.nextcloud import (
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
)
from dokploy_wizard.packs.nextcloud.models import NextcloudPhase
from dokploy_wizard.state import OwnershipLedger
from dokploy_wizard.uninstall.lifecycle_authority import LifecycleAuthorityPublisher
from dokploy_wizard.uninstall.lifecycle_authority_phases import (
    _publish_service,
    _publish_volume,
)


def publish_nextcloud_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: NextcloudPhase,
) -> None:
    if publisher is None:
        return
    nextcloud = phase.result.nextcloud
    onlyoffice = phase.result.onlyoffice
    if nextcloud is not None:
        _publish_service(
            publisher,
            ledger,
            NEXTCLOUD_SERVICE_RESOURCE_TYPE,
            nextcloud.service,
            phase.nextcloud_service_resource_id,
        )
        _publish_volume(
            publisher,
            ledger,
            NEXTCLOUD_VOLUME_RESOURCE_TYPE,
            nextcloud.data_volume,
            phase.nextcloud_volume_resource_id,
        )
    if onlyoffice is not None:
        _publish_service(
            publisher,
            ledger,
            ONLYOFFICE_SERVICE_RESOURCE_TYPE,
            onlyoffice.service,
            phase.onlyoffice_service_resource_id,
        )
        _publish_volume(
            publisher,
            ledger,
            ONLYOFFICE_VOLUME_RESOURCE_TYPE,
            onlyoffice.data_volume,
            phase.onlyoffice_volume_resource_id,
        )


def publish_moodle_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: MoodlePhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        MOODLE_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )
    _publish_volume(
        publisher,
        ledger,
        MOODLE_DATA_RESOURCE_TYPE,
        phase.result.persistent_data,
        phase.data_resource_id,
    )


def publish_docuseal_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: DocuSealPhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        DOCUSEAL_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )
    _publish_volume(
        publisher,
        ledger,
        DOCUSEAL_DATA_RESOURCE_TYPE,
        phase.result.persistent_data,
        phase.data_resource_id,
    )


def publish_coder_authority(
    publisher: LifecycleAuthorityPublisher | None,
    ledger: OwnershipLedger,
    phase: CoderPhase,
) -> None:
    if publisher is None:
        return
    _publish_service(
        publisher,
        ledger,
        CODER_SERVICE_RESOURCE_TYPE,
        phase.result.service,
        phase.service_resource_id,
    )
    _publish_volume(
        publisher,
        ledger,
        CODER_DATA_RESOURCE_TYPE,
        phase.result.persistent_data,
        phase.data_resource_id,
    )
