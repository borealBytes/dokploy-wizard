"""Typed mapping from planner resource labels to physical provider families."""

from __future__ import annotations

from enum import StrEnum

from dokploy_wizard.core import (
    SHARED_LITELLM_RESOURCE_TYPE,
    SHARED_MAIL_RELAY_RESOURCE_TYPE,
    SHARED_NETWORK_RESOURCE_TYPE,
    SHARED_POSTGRES_RESOURCE_TYPE,
    SHARED_REDIS_RESOURCE_TYPE,
)
from dokploy_wizard.networking import (
    ACCESS_APPLICATION_RESOURCE_TYPE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
    ACCESS_POLICY_RESOURCE_TYPE,
    DNS_RESOURCE_TYPE,
    TUNNEL_RESOURCE_TYPE,
)
from dokploy_wizard.packs.coder import CODER_DATA_RESOURCE_TYPE, CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.docuseal import (
    DOCUSEAL_DATA_RESOURCE_TYPE,
    DOCUSEAL_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.headscale import HEADSCALE_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.matrix import MATRIX_DATA_RESOURCE_TYPE, MATRIX_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.moodle import MOODLE_DATA_RESOURCE_TYPE, MOODLE_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.nextcloud import (
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
)
from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.seaweedfs import (
    SEAWEEDFS_DATA_RESOURCE_TYPE,
    SEAWEEDFS_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.surfsense import (
    SURFSENSE_DATA_RESOURCE_TYPE,
    SURFSENSE_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.state.shared_core_sync import SYNC_SCHEDULE_RESOURCE_TYPE
from dokploy_wizard.tailscale import TAILSCALE_NODE_RESOURCE_TYPE
from dokploy_wizard.uninstall.planner import _RULES


class ProviderFamily(StrEnum):
    SCHEDULE = "schedule"
    CLOUDFLARE = "cloudflare"
    TAILSCALE = "tailscale"
    DOKPLOY = "dokploy"
    DOCKER = "docker"


_FAMILY_BY_RESOURCE_TYPE: dict[str, ProviderFamily] = {
    SYNC_SCHEDULE_RESOURCE_TYPE: ProviderFamily.SCHEDULE,
    TAILSCALE_NODE_RESOURCE_TYPE: ProviderFamily.TAILSCALE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE: ProviderFamily.CLOUDFLARE,
    ACCESS_APPLICATION_RESOURCE_TYPE: ProviderFamily.CLOUDFLARE,
    ACCESS_POLICY_RESOURCE_TYPE: ProviderFamily.CLOUDFLARE,
    DNS_RESOURCE_TYPE: ProviderFamily.CLOUDFLARE,
    TUNNEL_RESOURCE_TYPE: ProviderFamily.CLOUDFLARE,
    SHARED_NETWORK_RESOURCE_TYPE: ProviderFamily.DOCKER,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE: ProviderFamily.DOCKER,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE: ProviderFamily.DOCKER,
    MOODLE_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    DOCUSEAL_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    SEAWEEDFS_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    SURFSENSE_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    CODER_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    MATRIX_DATA_RESOURCE_TYPE: ProviderFamily.DOCKER,
    SHARED_LITELLM_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    SHARED_MAIL_RELAY_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    SHARED_POSTGRES_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    SHARED_REDIS_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    OPENCLAW_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    NEXTCLOUD_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    MOODLE_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    DOCUSEAL_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    SEAWEEDFS_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    SURFSENSE_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    CODER_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    MATRIX_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
    HEADSCALE_SERVICE_RESOURCE_TYPE: ProviderFamily.DOKPLOY,
}


def family_for(resource_type: str) -> ProviderFamily:
    """Return the sole physical provider family authorized for a planner label."""

    try:
        return _FAMILY_BY_RESOURCE_TYPE[resource_type]
    except KeyError as error:
        raise RuntimeError(f"No physical provider family supports '{resource_type}'.") from error


if set(_FAMILY_BY_RESOURCE_TYPE) != set(_RULES):
    raise RuntimeError(
        "Provider-family mapping must cover exactly every uninstall planner resource."
    )
