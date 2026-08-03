from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_migration_operations import (
    CoderMigrationBlockedError,
    CoderMigrationDependencies,
    CoderMigrationOperations,
    CoderTemplateDeletionOperations,
    TemplateDeletionDependencies,
    TemplateDeletionRequest,
    WorkspaceDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderApiError,
    CoderBuild,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)


def _stopped_build() -> CoderBuild:
    return CoderBuild(
        id=CoderId("44444444-4444-4444-4444-444444444444"),
        build_number=1,
        transition="stop",
        status="stopped",
        created_at="2026-07-27T00:00:00Z",
    )


def _workspace(build: CoderBuild) -> CoderWorkspace:
    return CoderWorkspace(
        id=CoderId("33333333-3333-3333-3333-333333333333"),
        template_id=CoderId("11111111-1111-1111-1111-111111111111"),
        name="stopped-workspace",
        latest_build=build,
    )


class NoopCrashHook:
    def hit(self, point: str) -> None:
        del point


class TerminalDeleteApi:
    def __init__(self) -> None:
        self._stopped = _stopped_build()
        self._deleted = CoderBuild(
            id=CoderId("55555555-5555-5555-5555-555555555555"),
            build_number=2,
            transition="delete",
            status="deleted",
            created_at="2026-07-27T00:01:00Z",
        )
        self.deleted = False

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        del organization_id
        return ()

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        return () if self.deleted else (_workspace(self._stopped),)

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        del workspace_id
        return (self._stopped, self._deleted) if self.deleted else (self._stopped,)

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        del workspace_id
        self.deleted = True
        raise CoderApiError(status=404, message="not found", detail=None, validations=())


class TemplateAbsentApi:
    def __init__(self) -> None:
        self.template = CoderTemplate(
            id=CoderId("11111111-1111-1111-1111-111111111111"),
            organization_id=CoderId("22222222-2222-2222-2222-222222222222"),
            name="retired-template",
        )
        self.deleted = False

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        del organization_id
        return () if self.deleted else (self.template,)

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        return ()

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        del workspace_id
        return ()

    def delete_template(self, template_id: str) -> bytes:
        del template_id
        self.deleted = True
        raise CoderApiError(status=404, message="not found", detail=None, validations=())


def _store(tmp_path: Path) -> CoderMigrationReceiptStore:
    return CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "receipt-token")


def test_terminal_delete_404_requires_exact_delete_build_history(tmp_path: Path) -> None:
    # Given
    api = TerminalDeleteApi()
    operations = CoderMigrationOperations(
        CoderMigrationDependencies(
            api=api,
            receipt_store=_store(tmp_path),
            clock=lambda: "2026-07-27T00:00:00Z",
            crash_hook=NoopCrashHook(),
        )
    )

    # When
    receipt = operations.delete_workspace(
        WorkspaceDeletionRequest(
            operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            desired_fingerprint="a" * 64,
            workspace_id="33333333-3333-3333-3333-333333333333",
        )
    )

    # Then
    assert receipt.status == "completed"
    assert receipt.steps[0].status == "verified"
    assert receipt.steps[0].submitted_delete_build_id == CoderId(
        "55555555-5555-5555-5555-555555555555"
    )


def test_unreceipted_404_blocks_recovery(tmp_path: Path) -> None:
    # Given
    operations = CoderMigrationOperations(
        CoderMigrationDependencies(
            api=TerminalDeleteApi(),
            receipt_store=_store(tmp_path),
            clock=lambda: "2026-07-27T00:00:00Z",
            crash_hook=NoopCrashHook(),
        )
    )

    # When / Then
    with pytest.raises(CoderMigrationBlockedError):
        operations.recover_workspace_delete()


def test_template_delete_intent_allows_404_after_complete_absence_proof(
    tmp_path: Path,
) -> None:
    # Given
    api = TemplateAbsentApi()
    operations = CoderTemplateDeletionOperations(
        TemplateDeletionDependencies(
            api=api,
            receipt_store=_store(tmp_path),
            clock=lambda: "2026-07-27T00:00:00Z",
            crash_hook=NoopCrashHook(),
        )
    )

    # When
    receipt = operations.delete_template(
        TemplateDeletionRequest(
            operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            desired_fingerprint="a" * 64,
            organization_id="22222222-2222-2222-2222-222222222222",
            template_id="11111111-1111-1111-1111-111111111111",
        )
    )

    # Then
    assert receipt.status == "completed"
    assert receipt.steps[0].status == "verified"
