from __future__ import annotations

import errno

import pytest

from dokploy_wizard.litellm.catalog_persistence import CatalogPersistenceError
from dokploy_wizard.litellm.opencode_go_persistence_errors import persistence_failure


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (CatalogPersistenceError("existing state bytes are unknown"), "persistence_bytes"),
        (CatalogPersistenceError("existing generation bytes are unknown"), "persistence_bytes"),
        (CatalogPersistenceError("generation does not bind state"), "persistence_binding"),
        (CatalogPersistenceError("state file mode is invalid"), "persistence_mode"),
        (CatalogPersistenceError("state file is a symlink"), "persistence_contract"),
        (
            CatalogPersistenceError(f"atomic state write failed: {errno.EACCES}"),
            "persistence_write_permission",
        ),
        (
            CatalogPersistenceError(f"immutable generation write failed: {errno.EROFS}"),
            "persistence_write_read_only",
        ),
        (
            CatalogPersistenceError(f"atomic state write failed: {errno.ENOSPC}"),
            "persistence_write_space",
        ),
        (OSError(errno.EPERM, "fixture"), "persistence_write_permission"),
        (OSError(errno.EIO, "fixture"), "persistence_write_other"),
    ),
)
def test_persistence_failure_returns_only_fixed_category(
    failure: CatalogPersistenceError | OSError,
    expected: str,
) -> None:
    # Given / When
    category = persistence_failure(failure)

    # Then
    assert category == expected
