from __future__ import annotations

from typing import Final, Literal, TypeGuard, get_args

from dokploy_wizard.bootstrap import DokployBootstrapError
from dokploy_wizard.core import SharedCoreError
from dokploy_wizard.lifecycle import LifecycleDriftError
from dokploy_wizard.networking import CloudflareError
from dokploy_wizard.packs.coder import CoderError
from dokploy_wizard.packs.headscale import HeadscaleError
from dokploy_wizard.packs.matrix import MatrixError
from dokploy_wizard.packs.nextcloud import NextcloudError
from dokploy_wizard.packs.openclaw import OpenClawError
from dokploy_wizard.packs.seaweedfs import SeaweedFsError
from dokploy_wizard.preflight import PreflightError
from dokploy_wizard.state.models import StateValidationError
from dokploy_wizard.tailscale import TailscaleError

Task18ModifyFailureCategory = Literal[
    "bootstrap",
    "cloudflare",
    "coder",
    "headscale",
    "lifecycle_drift",
    "matrix",
    "nextcloud",
    "openclaw",
    "os_error",
    "preflight",
    "seaweedfs",
    "shared_core",
    "state_validation",
    "tailscale",
]
TASK18_MODIFY_FAILURE_CATEGORIES: Final[frozenset[str]] = frozenset(
    category
    for category in get_args(Task18ModifyFailureCategory)
    if isinstance(category, str)
)


def is_task18_modify_failure_category(
    value: str,
) -> TypeGuard[Task18ModifyFailureCategory]:
    return value in TASK18_MODIFY_FAILURE_CATEGORIES


def task18_modify_failure(error: BaseException) -> Task18ModifyFailureCategory:
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, StateValidationError):
        return "state_validation"
    if isinstance(error, PreflightError):
        return "preflight"
    if isinstance(error, DokployBootstrapError):
        return "bootstrap"
    if isinstance(error, CloudflareError):
        return "cloudflare"
    if isinstance(error, SharedCoreError):
        return "shared_core"
    if isinstance(error, TailscaleError):
        return "tailscale"
    if isinstance(error, HeadscaleError):
        return "headscale"
    if isinstance(error, CoderError):
        return "coder"
    if isinstance(error, LifecycleDriftError):
        return "lifecycle_drift"
    if isinstance(error, MatrixError):
        return "matrix"
    if isinstance(error, NextcloudError):
        return "nextcloud"
    if isinstance(error, OpenClawError):
        return "openclaw"
    if isinstance(error, SeaweedFsError):
        return "seaweedfs"
    raise ValueError("unsupported Task 18 modify failure")
