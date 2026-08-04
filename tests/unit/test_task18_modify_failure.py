from __future__ import annotations

import pytest

from dokploy_wizard.bootstrap import DokployBootstrapError
from dokploy_wizard.core import SharedCoreError
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
)
from dokploy_wizard.dokploy.coder_template_migration_runtime import (
    TemplateMigrationExecutionError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import WorkspaceCatalogSyncError
from dokploy_wizard.lifecycle import DriftReport, LifecycleDriftError
from dokploy_wizard.networking import CloudflareError
from dokploy_wizard.packs.coder import CoderError
from dokploy_wizard.packs.headscale import HeadscaleError
from dokploy_wizard.packs.matrix import MatrixError
from dokploy_wizard.packs.nextcloud import NextcloudError
from dokploy_wizard.packs.openclaw import OpenClawError
from dokploy_wizard.packs.seaweedfs import SeaweedFsError
from dokploy_wizard.preflight import PreflightError
from dokploy_wizard.proof.model_sync_task18_modify_failure import task18_modify_failure
from dokploy_wizard.state.models import StateValidationError
from dokploy_wizard.state.sync_schema import SyncStateError
from dokploy_wizard.state.upgrade_intent import StateUpgradeError
from dokploy_wizard.tailscale import TailscaleError


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (OSError("fixture"), "os_error"),
        (StateValidationError("fixture"), "state_validation"),
        (PreflightError("fixture"), "preflight"),
        (DokployBootstrapError("fixture"), "bootstrap"),
        (CloudflareError("fixture"), "cloudflare"),
        (SharedCoreError("fixture"), "shared_core"),
        (TailscaleError("fixture"), "tailscale"),
        (HeadscaleError("fixture"), "headscale"),
        (CoderError("fixture"), "coder"),
        (LifecycleDriftError("fixture", report=DriftReport(entries=())), "lifecycle_drift"),
        (MatrixError("fixture"), "matrix"),
        (NextcloudError("fixture"), "nextcloud"),
        (OpenClawError("fixture"), "openclaw"),
        (SeaweedFsError("fixture"), "seaweedfs"),
        (SyncStateError("fixture"), "sync_state"),
        (WorkspaceCatalogSyncError("fixture"), "workspace_catalog_sync"),
        (
            TemplateMigrationExecutionError("fixture"),
            "template_migration_execution",
        ),
        (CoderMigrationBlockedError("fixture"), "coder_migration_blocked"),
        (StateUpgradeError("fixture"), "state_upgrade"),
        (RuntimeError("fixture"), "unexpected"),
    ),
)
def test_task18_modify_failure_returns_only_exception_type_category(
    failure: BaseException,
    expected: str,
) -> None:
    # Given / When
    category = task18_modify_failure(failure)

    # Then
    assert category == expected
