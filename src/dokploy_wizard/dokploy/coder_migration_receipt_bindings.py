from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationStep,
    ReceiptSchemaError,
)


def validate_step_binding_unchanged(before: MigrationStep, after: MigrationStep) -> None:
    normalized = replace(
        after,
        status=before.status,
        post_inventory_sha256=before.post_inventory_sha256,
        request_sha256=before.request_sha256,
        response_sha256=before.response_sha256,
        submitted_delete_build_id=before.submitted_delete_build_id,
        submitted_delete_build_number=before.submitted_delete_build_number,
        error=before.error,
        updated_at=before.updated_at,
    )
    if normalized != before:
        raise ReceiptSchemaError("receipt transition changes an immutable binding")
