from __future__ import annotations

import pytest

from dokploy_wizard.litellm.model_admin_types import LiteLLMModelAdminConflict
from dokploy_wizard.litellm.opencode_go_sync_errors import model_admin_failure


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("create confirmation left no owned alias", "model_admin_confirm_create_absent"),
        (
            "create confirmation left multiple owned aliases",
            "model_admin_confirm_create_multiple",
        ),
        (
            "create confirmation is not an owned OpenCode Go alias",
            "model_admin_confirm_create_ownership",
        ),
        (
            "create confirmation owned projection mismatch",
            "model_admin_confirm_create_projection",
        ),
        ("patch confirmation left no owned alias", "model_admin_confirm_patch_absent"),
        (
            "patch confirmation left multiple owned aliases",
            "model_admin_confirm_patch_multiple",
        ),
        (
            "patch confirmation is not an owned OpenCode Go alias",
            "model_admin_confirm_patch_ownership",
        ),
        (
            "patch confirmation owned projection mismatch",
            "model_admin_confirm_patch_projection",
        ),
    ),
)
def test_model_admin_failure_classifies_inventory_confirmation_conflict(
    reason: str,
    expected: str,
) -> None:
    # Given
    failure = LiteLLMModelAdminConflict(reason)

    # When
    category = model_admin_failure(failure)

    # Then
    assert category == expected
