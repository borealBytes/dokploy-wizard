from __future__ import annotations

from typing import Final

_REMOTE_STDERR_PREFIX: Final = b"[remote:modify:stderr] "
_FAILURE_CATEGORIES: Final[dict[bytes, str]] = {
    b"dokploy_wizard.state.sync_schema.SyncStateError": "sync_state",
    b"dokploy_wizard.state.models.StateValidationError": "state_validation",
    b"dokploy_wizard.state.upgrade_intent.StateUpgradeError": "state_upgrade",
    b"dokploy_wizard.dokploy.workspace_catalog_sync_models.WorkspaceCatalogSyncError": (
        "workspace_catalog_sync"
    ),
    b"dokploy_wizard.dokploy.workspace_catalog_sync_models.TransactionBlockedError": (
        "workspace_catalog_sync"
    ),
    b"dokploy_wizard.dokploy.workspace_catalog_sync_catalog_fetch.CatalogUnavailableError": (
        "workspace_catalog_sync"
    ),
    b"dokploy_wizard.dokploy.coder_template_migration_runtime."
    b"TemplateMigrationExecutionError": "template_migration_execution",
    b"dokploy_wizard.dokploy.coder_migration_workspace_models.CoderMigrationBlockedError": (
        "coder_migration_blocked"
    ),
    b"dokploy_wizard.dokploy.coder_migration_types.CoderApiError": "coder_api",
    b"dokploy_wizard.dokploy.coder_migration_types.CoderProtocolError": "coder_protocol",
    b"dokploy_wizard.dokploy.coder_migration_api.CoderTransportError": "coder_transport",
}


def remote_fixed_failure(stderr: bytes) -> str | None:
    categories = {
        category
        for line in stderr.splitlines()
        if line.startswith(_REMOTE_STDERR_PREFIX)
        for category in (
            _FAILURE_CATEGORIES.get(
                line.removeprefix(_REMOTE_STDERR_PREFIX).partition(b": ")[0]
            ),
        )
        if category is not None
    }
    return categories.pop() if len(categories) == 1 else None
