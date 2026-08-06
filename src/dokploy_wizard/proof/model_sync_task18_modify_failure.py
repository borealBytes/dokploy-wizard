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
    "coder_active_plan",
    "coder_bootstrap",
    "coder_http",
    "coder_runtime_images",
    "coder_template_migration",
    "coder_workspace_secrets",
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
    "state_validation_checkpoint",
    "state_validation_docker_auth",
    "state_validation_dokploy_auth",
    "state_validation_lifecycle_binding",
    "state_validation_modify_stack_name",
    "state_validation_modify_unmodeled",
    "state_validation_modify_unsupported_keys",
    "state_validation_state_absent",
    "state_validation_task18_desired_changed",
    "state_validation_task18_mode_unsupported",
    "state_validation_task18_phases_unavailable",
    "state_validation_task18_raw_changed",
    "state_validation_task18_resume_missing_required",
    "state_upgrade",
    "state_upgrade_applied_fingerprint_mismatch",
    "state_upgrade_cas_mismatch",
    "state_upgrade_complete_binding_invalid",
    "state_upgrade_input_invalid",
    "state_upgrade_intent_invalid",
    "state_upgrade_owner_mismatch",
    "state_upgrade_recovery_invalid",
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
        message = str(error)
        if message == "Task 18 Host A model-sync resume is missing required phases.":
            return "state_validation_task18_resume_missing_required"
        if message == "Task 18 Host A model-sync lifecycle mode is unsupported.":
            return "state_validation_task18_mode_unsupported"
        if message == "Task 18 Host A model-sync upgrade raw input changed.":
            return "state_validation_task18_raw_changed"
        if message == "Task 18 Host A model-sync upgrade desired state changed.":
            return "state_validation_task18_desired_changed"
        if message == "Task 18 Host A model-sync upgrade phases are unavailable.":
            return "state_validation_task18_phases_unavailable"
        if message.startswith(("Dokploy mutation auth ", "Task 1 Dokploy auth ")):
            return "state_validation_dokploy_auth"
        if message.startswith(("Docker Hub authentication ", "Docker Hub login ")):
            return "state_validation_docker_auth"
        if message.startswith("Applied checkpoint "):
            return "state_validation_checkpoint"
        if message.startswith(
            ("Lifecycle stack ", "Persisted desired state does not match the locked ")
        ):
            return "state_validation_lifecycle_binding"
        if message.startswith("Requested modify operation "):
            return "state_validation_modify_unmodeled"
        if message.startswith("Unsupported mutable env keys "):
            return "state_validation_modify_unsupported_keys"
        if message.startswith("STACK_NAME changes "):
            return "state_validation_modify_stack_name"
        if message.startswith("Cannot modify before "):
            return "state_validation_state_absent"
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
        message = str(error)
        if message.startswith("Coder template migration failed closed."):
            return "coder_template_migration"
        if message.startswith(("Coder runtime image ", "Unable to persist Coder runtime image ")):
            return "coder_runtime_images"
        if message.startswith("Coder workspace secret "):
            return "coder_workspace_secrets"
        if message.startswith(
            (
                "Coder service image ",
                "Coder service name ",
                "Coder hostnames ",
                "Coder postgres inputs ",
                "Coder data ",
            )
        ):
            return "coder_active_plan"
        if message.startswith(
            ("Coder container ", "Coder bootstrap ", "Unable to determine Coder bootstrap ")
        ):
            return "coder_bootstrap"
        if message.startswith("Coder request "):
            return "coder_http"
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
        message = str(error)
        if message == _STATE_UPGRADE_RESUME_HASH_MISMATCH:
            return "state_upgrade_resume_hash_mismatch"
        if message.startswith("State upgrade intent ") or message.startswith(
            "State upgrade hash map "
        ):
            return "state_upgrade_intent_invalid"
        if message.startswith("State upgrade owner "):
            return "state_upgrade_owner_mismatch"
        if message == "State upgrade generation/token CAS mismatch.":
            return "state_upgrade_cas_mismatch"
        if message == "State upgrade input document is invalid.":
            return "state_upgrade_input_invalid"
        if message == "State upgrade applied fingerprint mismatch.":
            return "state_upgrade_applied_fingerprint_mismatch"
        if message == "State upgrade complete intent does not bind the stale applied image.":
            return "state_upgrade_complete_binding_invalid"
        if message.startswith("State upgrade recovery "):
            return "state_upgrade_recovery_invalid"
        return "state_upgrade"
    return "unexpected"
