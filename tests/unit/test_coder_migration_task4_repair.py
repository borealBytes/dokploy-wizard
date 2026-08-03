from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_migration_operations import (
    CoderMigrationDependencies,
    CoderMigrationOperations,
    CoderTemplateDeletionOperations,
    TemplateDeletionDependencies,
    TemplateDeletionRequest,
    WorkspaceDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    ReceiptSchemaError,
    new_migration_receipt,
    new_migration_step,
    receipt_bytes,
)
from dokploy_wizard.dokploy.coder_migration_receipt_validation import parse_receipt
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderBuildStatus,
    CoderBuildTransition,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)

_TEMPLATE_ID = CoderId("11111111-1111-1111-1111-111111111111")
_ORGANIZATION_ID = CoderId("22222222-2222-2222-2222-222222222222")
_WORKSPACE_ID = CoderId("33333333-3333-3333-3333-333333333333")
_STOPPED_BUILD_ID = CoderId("44444444-4444-4444-4444-444444444444")
_DELETE_BUILD_ID = CoderId("55555555-5555-5555-5555-555555555555")


def _build(
    identifier: CoderId,
    number: int,
    transition: CoderBuildTransition,
    status: CoderBuildStatus,
) -> CoderBuild:
    return CoderBuild(
        id=identifier,
        build_number=number,
        transition=transition,
        status=status,
        created_at="2026-07-27T00:00:00Z",
    )


class NoopCrashHook:
    def hit(self, point: str) -> None:
        del point


class TerminalWorkspaceApi:
    def __init__(self) -> None:
        self._stopped = _build(_STOPPED_BUILD_ID, 1, "stop", "stopped")
        self._deleting = _build(_DELETE_BUILD_ID, 2, "delete", "deleting")
        self._deleted = _build(_DELETE_BUILD_ID, 2, "delete", "deleted")
        self._list_calls = 0
        self.delete_calls = 0

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        del organization_id
        return ()

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        self._list_calls += 1
        if self._list_calls <= 2:
            return (CoderWorkspace(_WORKSPACE_ID, _TEMPLATE_ID, "task4-workspace", self._stopped),)
        if self._list_calls == 3:
            return (CoderWorkspace(_WORKSPACE_ID, _TEMPLATE_ID, "task4-workspace", self._deleting),)
        return ()

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        del workspace_id
        if self._list_calls <= 2:
            return (self._stopped,)
        if self._list_calls == 3:
            return self._stopped, self._deleting
        return self._stopped, self._deleted

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        assert workspace_id == _WORKSPACE_ID
        self.delete_calls += 1
        return self._deleting


class AbsentTemplateApi:
    def __init__(self) -> None:
        self.template = CoderTemplate(_TEMPLATE_ID, _ORGANIZATION_ID, "task4-template")
        self.deleted = False
        self.delete_calls = 0

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        assert organization_id == _ORGANIZATION_ID
        return () if self.deleted else (self.template,)

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        return ()

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        del workspace_id
        return ()

    def delete_template(self, template_id: str) -> bytes:
        assert template_id == _TEMPLATE_ID
        self.delete_calls += 1
        self.deleted = True
        return b""


def _store(tmp_path: Path) -> CoderMigrationReceiptStore:
    return CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "receipt-token")


def test_workspace_delete_waits_for_exact_terminal_delete_build(tmp_path: Path) -> None:
    # Given
    api = TerminalWorkspaceApi()
    operations = CoderMigrationOperations(
        CoderMigrationDependencies(
            api,
            _store(tmp_path),
            lambda: "2026-07-27T00:00:00Z",
            NoopCrashHook(),
        )
    )

    # When
    receipt = operations.delete_workspace(
        WorkspaceDeletionRequest(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "a" * 64,
            str(_WORKSPACE_ID),
        )
    )

    # Then
    assert receipt.status == "completed"
    assert receipt.steps[0].status == "verified"
    assert receipt.steps[0].submitted_delete_build_id == _DELETE_BUILD_ID
    assert api.delete_calls == 1


def test_template_delete_200_requires_complete_absence_proof(tmp_path: Path) -> None:
    # Given
    api = AbsentTemplateApi()
    operations = CoderTemplateDeletionOperations(
        TemplateDeletionDependencies(
            api,
            _store(tmp_path),
            lambda: "2026-07-27T00:00:00Z",
            NoopCrashHook(),
        )
    )

    # When
    receipt = operations.delete_template(
        TemplateDeletionRequest(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "a" * 64,
            str(_ORGANIZATION_ID),
            str(_TEMPLATE_ID),
        )
    )

    # Then
    assert receipt.status == "completed"
    assert receipt.steps[0].status == "verified"
    assert api.delete_calls == 1


def test_rejects_workspace_intent_without_complete_predelete_sequence() -> None:
    # Given
    step = replace(
        new_migration_step(
            step_id="delete-workspace-33333333-3333-3333-3333-333333333333",
            kind="delete_workspace",
            status="intent",
            created_at="2026-07-27T00:00:00Z",
        ),
        template_id=_TEMPLATE_ID,
        workspace_id=_WORKSPACE_ID,
        name="task4-workspace",
        latest_build_id=_STOPPED_BUILD_ID,
        latest_build_number=1,
        latest_build_status="stopped",
        stopped=True,
        desired_fingerprint="a" * 64,
        pre_inventory_sha256="b" * 64,
    )
    receipt = replace(
        new_migration_receipt(
            operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            desired_fingerprint="a" * 64,
            pre_inventory_sha256="b" * 64,
            created_at="2026-07-27T00:00:00Z",
            step=step,
        ),
        cas_token="receipt-token",
    )

    # When / Then
    with pytest.raises(ReceiptSchemaError):
        parse_receipt(receipt_bytes(receipt))


def test_receipt_store_reads_a_canonical_receipt_across_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    store = _store(tmp_path)
    stored = store.create(
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
    read = os.read

    def short_read(descriptor: int, count: int) -> bytes:
        return read(descriptor, min(count, 1))

    monkeypatch.setattr(os, "read", short_read)

    # When
    loaded = store.load()

    # Then
    assert loaded == stored
