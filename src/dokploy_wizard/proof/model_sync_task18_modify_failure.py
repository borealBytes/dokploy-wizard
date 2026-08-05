from __future__ import annotations

from typing import Final, Literal, TypeGuard, get_args

from dokploy_wizard.bootstrap import DokployBootstrapError
from dokploy_wizard.core import SharedCoreError
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
)
from dokploy_wizard.dokploy.coder_template_migration_runtime import (
    TemplateMigrationExecutionError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import WorkspaceCatalogSyncError
from dokploy_wizard.lifecycle import LifecycleDriftError
from dokploy_wizard.lifecycle.lock import LifecycleLockBusyError
from dokploy_wizard.networking import CloudflareError
from dokploy_wizard.packs.coder import CoderError
from dokploy_wizard.packs.headscale import HeadscaleError
from dokploy_wizard.packs.matrix import MatrixError
from dokploy_wizard.packs.nextcloud import NextcloudError
from dokploy_wizard.packs.openclaw import OpenClawError
from dokploy_wizard.packs.seaweedfs import SeaweedFsError
from dokploy_wizard.preflight import PreflightError
from dokploy_wizard.state.models import StateValidationError
from dokploy_wizard.state.sync_schema import SyncStateError
from dokploy_wizard.state.upgrade_intent import StateUpgradeError
from dokploy_wizard.tailscale import TailscaleError

Task18ModifyFailureCategory = Literal[
    "bootstrap",
    "cloudflare",
    "coder",
    "headscale",
    "lifecycle_drift",
    "lifecycle_lock",
    "matrix",
    "nextcloud",
    "openclaw",
    "os_error",
    "preflight",
    "seaweedfs",
    "shared_core",
    "state_validation",
    "state_upgrade",
    "state_upgrade_resume_hash_mismatch",
    "sync_state",
    "system_exit",
    "tailscale",
    "template_migration_execution",
    "unexpected",
    "workspace_catalog_sync",
    "coder_migration_blocked",
]
_STATE_UPGRADE_RESUME_HASH_MISMATCH: Final = "State upgrade resume hash mismatch."
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
    if isinstance(error, SystemExit):
        return "system_exit"
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
    if isinstance(error, LifecycleLockBusyError):
        return "lifecycle_lock"
    if isinstance(error, MatrixError):
        return "matrix"
    if isinstance(error, NextcloudError):
        return "nextcloud"
    if isinstance(error, OpenClawError):
        return "openclaw"
    if isinstance(error, SeaweedFsError):
        return "seaweedfs"
    if isinstance(error, SyncStateError):
        return "sync_state"
    if isinstance(error, WorkspaceCatalogSyncError):
        return "workspace_catalog_sync"
    if isinstance(error, TemplateMigrationExecutionError):
        return "template_migration_execution"
    if isinstance(error, CoderMigrationBlockedError):
        return "coder_migration_blocked"
    if isinstance(error, StateUpgradeError):
        if str(error) == _STATE_UPGRADE_RESUME_HASH_MISMATCH:
            return "state_upgrade_resume_hash_mismatch"
        return "state_upgrade"
    return "unexpected"
