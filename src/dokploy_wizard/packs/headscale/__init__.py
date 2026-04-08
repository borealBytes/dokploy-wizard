"""Public Headscale runtime interface."""

from dokploy_wizard.packs.headscale.models import (
    HeadscaleHealthCheck,
    HeadscaleManagedResource,
    HeadscalePhase,
    HeadscaleResourceRecord,
    HeadscaleResult,
)
from dokploy_wizard.packs.headscale.reconciler import (
    HEADSCALE_SERVICE_RESOURCE_TYPE,
    HeadscaleBackend,
    HeadscaleError,
    ShellHeadscaleBackend,
    build_headscale_ledger,
    reconcile_headscale,
)

__all__ = [
    "HEADSCALE_SERVICE_RESOURCE_TYPE",
    "HeadscaleBackend",
    "HeadscaleError",
    "HeadscaleHealthCheck",
    "HeadscaleManagedResource",
    "HeadscalePhase",
    "HeadscaleResourceRecord",
    "HeadscaleResult",
    "ShellHeadscaleBackend",
    "build_headscale_ledger",
    "reconcile_headscale",
]
