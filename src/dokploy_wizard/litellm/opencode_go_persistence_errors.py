from __future__ import annotations

import errno

from dokploy_wizard.litellm.catalog_persistence import CatalogPersistenceError
from dokploy_wizard.litellm.opencode_go_sync_errors import SyncFailureCategory

_ERRNO_CATEGORIES: dict[int, SyncFailureCategory] = {
    errno.EACCES: "persistence_write_permission",
    errno.EPERM: "persistence_write_permission",
    errno.EROFS: "persistence_write_read_only",
    errno.ENOSPC: "persistence_write_space",
}


def persistence_failure(error: CatalogPersistenceError | OSError) -> SyncFailureCategory:
    if isinstance(error, OSError):
        if error.errno is None:
            return "persistence_write_other"
        return _ERRNO_CATEGORIES.get(error.errno, "persistence_write_other")

    reason = error.reason
    for error_number, category in _ERRNO_CATEGORIES.items():
        if reason.endswith(f": {error_number}"):
            return category
    if "write failed" in reason:
        return "persistence_write_other"
    if reason == "existing state bytes are unknown":
        return "persistence_state_bytes"
    if reason == "existing generation bytes are unknown":
        return "persistence_generation_bytes"
    if "bind state" in reason:
        return "persistence_binding"
    if "mode" in reason:
        return "persistence_mode"
    return "persistence_contract"
