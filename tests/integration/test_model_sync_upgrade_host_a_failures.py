from __future__ import annotations

import pytest

from dokploy_wizard.proof.model_sync_upgrade_host_a_process import (
    ProcessObservation,
    parse_modify_command,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError


def test_modify_command_rejects_success_without_remote_summary() -> None:
    # Given
    process = ProcessObservation(0, b"", b"[remote] completed remote modify\n")

    # When / Then
    with pytest.raises(UpgradeHostAError, match="summary"):
        parse_modify_command(process)


@pytest.mark.parametrize("category", ("model_admin_http_400", "model_admin_conflict"))
def test_modify_command_preserves_allowlisted_remote_sync_category(category: str) -> None:
    # Given
    process = ProcessObservation(
        1,
        b"",
        (
            b"[remote:modify:stderr] dokploy_wizard.state.sync_schema.SyncStateError: "
            b"Immediate OpenCode Go sync command failed: " + category.encode("ascii") + b".\n"
        ),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match=category):
        parse_modify_command(process)


@pytest.mark.parametrize(
    ("failure_type", "category"),
    (
        ("dokploy_wizard.state.sync_schema.SyncStateError", "sync_state"),
        (
            "dokploy_wizard.dokploy.workspace_catalog_sync_models."
            "WorkspaceCatalogSyncError",
            "workspace_catalog_sync",
        ),
        (
            "dokploy_wizard.dokploy.workspace_catalog_sync_models."
            "TransactionBlockedError",
            "workspace_catalog_sync",
        ),
        (
            "dokploy_wizard.dokploy.workspace_catalog_sync_catalog_fetch."
            "CatalogUnavailableError",
            "workspace_catalog_sync",
        ),
        (
            "dokploy_wizard.dokploy.coder_template_migration_runtime."
            "TemplateMigrationExecutionError",
            "template_migration_execution",
        ),
        (
            "dokploy_wizard.dokploy.coder_migration_workspace_models."
            "CoderMigrationBlockedError",
            "coder_migration_blocked",
        ),
        ("dokploy_wizard.dokploy.coder_migration_types.CoderApiError", "coder_api"),
        (
            "dokploy_wizard.dokploy.coder_migration_types.CoderProtocolError",
            "coder_protocol",
        ),
        (
            "dokploy_wizard.dokploy.coder_migration_api.CoderTransportError",
            "coder_transport",
        ),
        ("dokploy_wizard.state.models.StateValidationError", "state_validation"),
        ("dokploy_wizard.state.upgrade_intent.StateUpgradeError", "state_upgrade"),
    ),
)
def test_modify_command_preserves_only_fixed_remote_failure_type(
    failure_type: str,
    category: str,
) -> None:
    # Given
    process = ProcessObservation(
        1,
        b"",
        f"[remote:modify:stderr] {failure_type}: fixture detail\n".encode(),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match=category):
        parse_modify_command(process)


@pytest.mark.parametrize(
    ("stderr", "category"),
    (
        (
            b"[remote:modify-observation-before:stderr] fixture detail\n",
            "observation_before",
        ),
        (
            b"[remote:modify-observation-after:stderr] fixture detail\n",
            "observation_after",
        ),
        (
            b'[remote:modify:stdout] {"lifecycle":{"mode":"modify",'
            b'"phases_to_run":["shared_core"]}}\n',
            "modify_nonzero_summary",
        ),
        (
            b"[remote:modify:stderr] DOKPLOY_WIZARD_TASK18_ERROR=shared_core\n",
            "shared_core",
        ),
        (
            b"[remote:modify-observation-before:stdout] {}\n"
            b"[remote:modify-observation-after:stdout] {}\n",
            "modify_command_unclassified",
        ),
        (
            b"[remote:modify-observation-before:stdout] {}\n",
            "observation_after",
        ),
        (b"[remote] archive failed\n", "wrapper_setup"),
    ),
)
def test_modify_command_preserves_only_fixed_remote_failure_structure(
    stderr: bytes,
    category: str,
) -> None:
    # Given
    process = ProcessObservation(1, b"", stderr)

    # When / Then
    with pytest.raises(UpgradeHostAError, match=category):
        parse_modify_command(process)


def test_modify_command_parses_noop_lifecycle_from_verbose_remote_output() -> None:
    # Given
    lines = (
        "[remote:modify:stdout] {",
        '[remote:modify:stdout]   "lifecycle": {',
        '[remote:modify:stdout]     "mode": "noop",',
        '[remote:modify:stdout]     "phases_to_run": []',
        "[remote:modify:stdout]   }",
        "[remote:modify:stdout] }",
    )

    # When
    result = parse_modify_command(ProcessObservation(0, b"", "\n".join(lines).encode()))

    # Then
    assert result.exit_code == 0
    assert result.failure_code is None
    assert result.lifecycle_mode == "noop"
    assert result.phases_to_run == ()
