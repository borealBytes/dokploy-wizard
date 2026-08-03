from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_migration_receipts import (
    CoderMigrationReceiptStore,
    ReceiptConcurrencyError,
    ReceiptSchemaError,
    ReceiptUpdate,
    new_migration_receipt,
    new_migration_step,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderId


def test_persists_canonical_receipt_with_explicit_inapplicable_nulls(tmp_path: Path) -> None:
    # Given
    store = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "first-token")
    receipt = new_migration_receipt(
        operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        desired_fingerprint="a" * 64,
        pre_inventory_sha256="b" * 64,
        created_at="2026-07-27T00:00:00Z",
        step=new_migration_step(
            step_id="inventory-1",
            kind="inventory",
            status="pending",
            created_at="2026-07-27T00:00:00Z",
        ),
    )

    # When
    stored = store.create(receipt)

    # Then
    path = tmp_path / "coder-migration-receipts-v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert stored.generation == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    assert payload["post_inventory_sha256"] is None
    assert payload["steps"][0]["workspace_id"] is None
    assert payload["steps"][0]["submitted_delete_build_id"] is None


def test_rejects_stale_generation_and_token_compare_and_swap(tmp_path: Path) -> None:
    # Given
    tokens = iter(("first-token", "second-token"))
    store = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: next(tokens))
    original = store.create(
        new_migration_receipt(
            operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            desired_fingerprint="a" * 64,
            pre_inventory_sha256="b" * 64,
            created_at="2026-07-27T00:00:00Z",
            step=new_migration_step(
                step_id="inventory-1",
                kind="inventory",
                status="pending",
                created_at="2026-07-27T00:00:00Z",
            ),
        )
    )

    # When
    current = store.checkpoint(
        original,
        ReceiptUpdate(
            status="running",
            updated_at="2026-07-27T00:01:00Z",
            steps=original.steps,
            post_inventory_sha256=None,
        ),
    )

    # Then
    assert current.generation == 1
    assert current.cas_token == "second-token"
    with pytest.raises(ReceiptConcurrencyError):
        store.checkpoint(
            original,
            ReceiptUpdate(
                status="blocked",
                updated_at="2026-07-27T00:02:00Z",
                steps=original.steps,
                post_inventory_sha256=None,
            ),
        )


def test_rejects_unknown_existing_receipt_bytes(tmp_path: Path) -> None:
    # Given
    receipt_path = tmp_path / "coder-migration-receipts-v1.json"
    receipt_path.write_text("{}", encoding="utf-8")
    receipt_path.chmod(0o600)
    store = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "first-token")

    # When / Then
    with pytest.raises(ValueError):
        store.load()


def test_template_mutation_checkpoint_cannot_rewrite_uuid_binding(tmp_path: Path) -> None:
    # Given
    step = replace(
        new_migration_step(
            step_id="rename-template-primary",
            kind="rename_template",
            status="intent",
            created_at="2026-07-31T00:00:00Z",
        ),
        template_id=CoderId("11111111-1111-1111-1111-111111111111"),
        organization_id=CoderId("22222222-2222-2222-2222-222222222222"),
        name="ubuntu-vscode-opencode-pi",
        desired_fingerprint="a" * 64,
        pre_inventory_sha256="b" * 64,
    )
    store = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "receipt-token")
    stored = store.create(
        replace(
            new_migration_receipt(
                operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                desired_fingerprint="a" * 64,
                pre_inventory_sha256="b" * 64,
                created_at="2026-07-31T00:00:00Z",
                step=step,
            ),
            steps=(step,),
        )
    )
    rewritten = replace(
        step,
        template_id=CoderId("33333333-3333-3333-3333-333333333333"),
        status="submitted",
        request_sha256="c" * 64,
        response_sha256="d" * 64,
    )

    # When / Then
    with pytest.raises(ReceiptSchemaError, match="immutable binding"):
        store.checkpoint(
            stored,
            ReceiptUpdate(
                status="running",
                updated_at="2026-07-31T00:00:00Z",
                steps=(rewritten,),
                post_inventory_sha256=None,
            ),
        )


def test_absent_push_intent_persists_null_uuid_with_digest_and_absence_proof(
    tmp_path: Path,
) -> None:
    # Given
    step = replace(
        new_migration_step(
            step_id="push-template-ubuntu-vscode-opencode-web",
            kind="push_template",
            status="intent",
            created_at="2026-07-31T00:00:00Z",
        ),
        organization_id=CoderId("22222222-2222-2222-2222-222222222222"),
        name="ubuntu-vscode-opencode-web",
        desired_fingerprint="a" * 64,
        pre_inventory_sha256="b" * 64,
        rendered_sha256="c" * 64,
        runtime_lock_sha256="d" * 64,
        template_version_name="dokploy-wizard-cccccccccccccccc",
    )
    store = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "receipt-token")

    # When
    stored = store.create(
        replace(
            new_migration_receipt(
                operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                desired_fingerprint="a" * 64,
                pre_inventory_sha256="b" * 64,
                created_at="2026-07-31T00:00:00Z",
                step=step,
            ),
            steps=(step,),
        )
    )

    # Then
    assert stored.steps[0].template_id is None
    assert stored.steps[0].pre_inventory_sha256 == "b" * 64
    assert stored.steps[0].rendered_sha256 == "c" * 64
    assert stored.steps[0].runtime_lock_sha256 == "d" * 64
