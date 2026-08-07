from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from dokploy_wizard import cli
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
from dokploy_wizard.lifecycle.lock import LifecycleLockBusyError
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
        (
            StateValidationError(
                "Task 18 Host A model-sync resume is missing required phases."
            ),
            "state_validation_task18_resume_missing_required",
        ),
        (
            StateValidationError("Task 18 Host A model-sync lifecycle mode is unsupported."),
            "state_validation_task18_mode_unsupported",
        ),
        (
            StateValidationError("Task 18 Host A model-sync upgrade raw input changed."),
            "state_validation_task18_raw_changed",
        ),
        (
            StateValidationError("Task 18 Host A model-sync upgrade desired state changed."),
            "state_validation_task18_desired_changed",
        ),
        (
            StateValidationError("Task 18 Host A model-sync upgrade phases are unavailable."),
            "state_validation_task18_phases_unavailable",
        ),
        (
            StateValidationError("Dokploy mutation auth qualification requires a usable key."),
            "state_validation_dokploy_auth",
        ),
        (
            StateValidationError("Docker Hub login failed."),
            "state_validation_docker_auth",
        ),
        (
            StateValidationError("Applied checkpoint does not match the lifecycle phase order."),
            "state_validation_checkpoint",
        ),
        (
            StateValidationError("Lifecycle stack binding schema is invalid."),
            "state_validation_lifecycle_binding",
        ),
        (
            StateValidationError("Requested modify operation changes values that are not modeled."),
            "state_validation_modify_unmodeled",
        ),
        (
            StateValidationError(
                "Requested modify operation changes values that are not modeled. "
                "Category: inactive_dokploy_admin."
            ),
            "state_validation_modify_inactive_dokploy_admin",
        ),
        (
            StateValidationError(
                "Requested modify operation changes values that are not modeled. "
                "Category: disabled_tailscale."
            ),
            "state_validation_modify_disabled_tailscale",
        ),
        (
            StateValidationError(
                "Requested modify operation changes values that are not modeled. "
                "Category: inactive_pack."
            ),
            "state_validation_modify_inactive_pack",
        ),
        (
            StateValidationError("Unsupported mutable env keys for Task 11."),
            "state_validation_modify_unsupported_keys",
        ),
        (
            StateValidationError("STACK_NAME changes are unsupported in Task 11."),
            "state_validation_modify_stack_name",
        ),
        (
            StateValidationError("Cannot modify before state exists."),
            "state_validation_state_absent",
        ),
        (PreflightError("fixture"), "preflight"),
        (DokployBootstrapError("fixture"), "bootstrap"),
        (CloudflareError("fixture"), "cloudflare"),
        (SharedCoreError("fixture"), "shared_core"),
        (TailscaleError("fixture"), "tailscale"),
        (HeadscaleError("fixture"), "headscale"),
        (CoderError("fixture"), "coder"),
        (CoderError("Coder template migration failed closed."), "coder_template_migration"),
        (CoderError("Coder runtime image state is unavailable."), "coder_runtime_images"),
        (
            CoderError("Coder workspace secret specification is invalid."),
            "coder_workspace_secrets_specification",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed."),
            "coder_workspace_secrets_reconciliation",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt"),
            "coder_workspace_secrets_reconciliation_receipt",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. unknown"),
            "coder_workspace_secrets_reconciliation_unknown",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. blocked"),
            "coder_workspace_secrets_reconciliation_blocked",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. client"),
            "coder_workspace_secrets_reconciliation_client",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. metadata"),
            "coder_workspace_secrets_reconciliation_metadata",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_invalid"),
            "coder_workspace_secrets_reconciliation_receipt_invalid",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_owner"),
            "coder_workspace_secrets_reconciliation_receipt_owner",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read"),
            "coder_workspace_secrets_reconciliation_receipt_read",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_directory"),
            "coder_workspace_secrets_reconciliation_receipt_read_directory",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_file"),
            "coder_workspace_secrets_reconciliation_receipt_read_file",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_json"),
            "coder_workspace_secrets_reconciliation_receipt_read_json",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_schema"),
            "coder_workspace_secrets_reconciliation_receipt_read_schema",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_schema_fields"),
            "coder_workspace_secrets_reconciliation_receipt_read_schema_fields",
        ),
        (
            CoderError(
                "Coder workspace secret reconciliation failed. receipt_read_schema_lifecycle"
            ),
            "coder_workspace_secrets_reconciliation_receipt_read_schema_lifecycle",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_schema_order"),
            "coder_workspace_secrets_reconciliation_receipt_read_schema_order",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_schema_value"),
            "coder_workspace_secrets_reconciliation_receipt_read_schema_value",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_read_schema_version"),
            "coder_workspace_secrets_reconciliation_receipt_read_schema_version",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_schema"),
            "coder_workspace_secrets_reconciliation_receipt_schema",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. receipt_write"),
            "coder_workspace_secrets_reconciliation_receipt_write",
        ),
        (
            CoderError("Coder workspace secret reconciliation failed. reconciliation"),
            "coder_workspace_secrets_reconciliation_internal",
        ),
        (CoderError("Coder service image does not match the plan."), "coder_active_plan"),
        (CoderError("Coder bootstrap API did not become ready."), "coder_bootstrap"),
        (CoderError("Coder request GET /api returned HTTP 500."), "coder_http"),
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
        (
            StateUpgradeError("State upgrade resume hash mismatch."),
            "state_upgrade_resume_hash_mismatch",
        ),
        (
            StateUpgradeError("State upgrade intent has an unsupported schema."),
            "state_upgrade_intent_invalid",
        ),
        (
            StateUpgradeError("State upgrade owner does not match the requested owner."),
            "state_upgrade_owner_mismatch",
        ),
        (
            StateUpgradeError("State upgrade generation/token CAS mismatch."),
            "state_upgrade_cas_mismatch",
        ),
        (
            StateUpgradeError("State upgrade input document is invalid."),
            "state_upgrade_input_invalid",
        ),
        (
            StateUpgradeError("State upgrade applied fingerprint mismatch."),
            "state_upgrade_applied_fingerprint_mismatch",
        ),
        (
            StateUpgradeError("State upgrade recovery target does not match the intent."),
            "state_upgrade_recovery_invalid",
        ),
        (
            StateUpgradeError(
                "State upgrade complete intent does not bind the stale applied image."
            ),
            "state_upgrade_complete_binding_invalid",
        ),
        (LifecycleLockBusyError("fixture"), "lifecycle_lock"),
        (SystemExit(1), "system_exit"),
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


def test_handle_modify_emits_task18_marker_for_busy_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    def raise_busy_lock(*_args: object, **_kwargs: object) -> None:
        raise LifecycleLockBusyError("fixture")

    monkeypatch.setattr(cli, "_load_install_raw_env", raise_busy_lock)
    arguments = argparse.Namespace(
        env_file=Path("fixture.env"),
        non_interactive=True,
        dry_run=False,
        task18_force_model_sync_upgrade=True,
    )

    # When
    with pytest.raises(SystemExit):
        cli._handle_modify(arguments)

    # Then
    assert capsys.readouterr().err == "DOKPLOY_WIZARD_TASK18_ERROR=lifecycle_lock\n"
