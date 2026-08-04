from __future__ import annotations

from typing import Final

from dokploy_wizard.proof.model_sync_task18_modify_failure import (
    is_task18_modify_failure_category,
)

_REMOTE_STDERR_PREFIX: Final = b"[remote:modify:stderr] "
_TASK18_ERROR_PREFIX: Final = b"DOKPLOY_WIZARD_TASK18_ERROR="
_PHASE_FAILURE_CATEGORIES: Final[dict[bytes, str]] = {
    b"[remote:modify-observation-before:stderr] ": "observation_before",
    b"[remote:modify-observation-after:stderr] ": "observation_after",
}
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
        for category in _line_failure_categories(line)
        if category is not None
    }
    return categories.pop() if len(categories) == 1 else None


def _line_failure_categories(line: bytes) -> tuple[str | None, ...]:
    fixed_type = None
    if line.startswith(_REMOTE_STDERR_PREFIX):
        remote_line = line.removeprefix(_REMOTE_STDERR_PREFIX)
        fixed_type = _FAILURE_CATEGORIES.get(remote_line.partition(b": ")[0])
        if remote_line.startswith(_TASK18_ERROR_PREFIX):
            candidate = remote_line.removeprefix(_TASK18_ERROR_PREFIX).decode("ascii")
            if is_task18_modify_failure_category(candidate):
                fixed_type = candidate
    return (
        fixed_type,
        *(
            category
            for prefix, category in _PHASE_FAILURE_CATEGORIES.items()
            if line.startswith(prefix)
        ),
    )
