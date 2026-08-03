from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_migration_operations import (
    CoderMigrationBlockedError,
    CoderMigrationDependencies,
    CoderMigrationOperations,
    WorkspaceDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderApiError,
    CoderBuild,
    CoderBuildStatus,
    CoderBuildTransition,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)


def _build(
    identifier: CoderId, number: int, transition: CoderBuildTransition, status: CoderBuildStatus
) -> CoderBuild:
    return CoderBuild(
        id=identifier,
        build_number=number,
        transition=transition,
        status=status,
        created_at="2026-07-27T00:00:00Z",
    )


def _workspace(build: CoderBuild) -> CoderWorkspace:
    return CoderWorkspace(
        id=CoderId("33333333-3333-3333-3333-333333333333"),
        template_id=CoderId("11111111-1111-1111-1111-111111111111"),
        name="stopped-workspace",
        latest_build=build,
    )


class FakeMigrationApi:
    def __init__(self, before: tuple[CoderBuild, ...], after: tuple[CoderBuild, ...]) -> None:
        self.before = before
        self.after = after
        self.list_calls = 0
        self.delete_calls = 0
        self.workspace_present = True

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        del organization_id
        return ()

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        self.list_calls += 1
        if not self.workspace_present:
            return ()
        builds = self.before if self.list_calls == 1 else self.after
        return (_workspace(builds[-1]),)

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        del workspace_id
        return self.before if self.list_calls == 1 else self.after

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        del workspace_id
        self.delete_calls += 1
        return _build(CoderId("55555555-5555-5555-5555-555555555555"), 2, "delete", "deleting")


class ConflictMigrationApi(FakeMigrationApi):
    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        del workspace_id
        self.delete_calls += 1
        raise CoderApiError(status=409, message="conflict", detail=None, validations=())


class CrashAfterResponse:
    def hit(self, point: str) -> None:
        if point == "after_response":
            raise SystemExit(17)


class NoopCrashHook:
    def hit(self, point: str) -> None:
        del point


class CrashBeforeRequest:
    def hit(self, point: str) -> None:
        if point == "before_request":
            raise SystemExit(18)


class CrashOnSecondCheckpoint:
    def __init__(self, point: str) -> None:
        self._point = point
        self._hits = 0

    def hit(self, point: str) -> None:
        if point == self._point:
            self._hits += 1
            if self._hits == 2:
                raise SystemExit(19)


def _operations(
    tmp_path: Path,
    api: FakeMigrationApi,
    hook: CrashAfterResponse | CrashBeforeRequest | CrashOnSecondCheckpoint | NoopCrashHook,
) -> CoderMigrationOperations:
    return CoderMigrationOperations(
        CoderMigrationDependencies(
            api=api,
            receipt_store=CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "token"),
            clock=lambda: "2026-07-27T00:00:00Z",
            crash_hook=hook,
        )
    )


def _request() -> WorkspaceDeletionRequest:
    return WorkspaceDeletionRequest(
        operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        desired_fingerprint="a" * 64,
        workspace_id="33333333-3333-3333-3333-333333333333",
    )


def test_delete_crash_boundary_after_response_preserves_intent(tmp_path: Path) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    api = FakeMigrationApi((stopped,), (stopped,))
    operations = _operations(tmp_path, api, CrashAfterResponse())

    # When / Then
    with pytest.raises(SystemExit):
        operations.delete_workspace(_request())
    receipt = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "unused").load()
    assert receipt is not None
    assert receipt.steps[0].status == "intent"
    assert api.delete_calls == 1


def test_intervening_build_blocks_concurrent_start_before_delete(tmp_path: Path) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    restarted = _build(CoderId("55555555-5555-5555-5555-555555555555"), 2, "start", "running")
    api = FakeMigrationApi((stopped,), (stopped, restarted))
    operations = _operations(tmp_path, api, NoopCrashHook())

    # When / Then
    with pytest.raises(CoderMigrationBlockedError):
        operations.delete_workspace(_request())
    assert api.delete_calls == 0


def test_http_409_forces_full_reinventory_before_blocking(tmp_path: Path) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    api = ConflictMigrationApi((stopped,), (stopped,))
    operations = _operations(tmp_path, api, NoopCrashHook())

    # When / Then
    with pytest.raises(CoderMigrationBlockedError):
        operations.delete_workspace(_request())
    assert api.delete_calls == 1
    assert api.list_calls == 3


def test_delete_crash_boundary_before_request_preserves_intent(tmp_path: Path) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    api = FakeMigrationApi((stopped,), (stopped,))
    operations = _operations(tmp_path, api, CrashBeforeRequest())

    # When / Then
    with pytest.raises(SystemExit):
        operations.delete_workspace(_request())
    receipt = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "unused").load()
    assert receipt is not None
    assert receipt.steps[0].status == "intent"
    assert api.delete_calls == 0


def test_delete_intent_recovery_accepts_exact_terminal_delete_after_response_crash(
    tmp_path: Path,
) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    deleted = _build(CoderId("55555555-5555-5555-5555-555555555555"), 2, "delete", "deleted")
    api = FakeMigrationApi((stopped,), (stopped,))
    crashing = _operations(tmp_path, api, CrashAfterResponse())
    with pytest.raises(SystemExit):
        crashing.delete_workspace(_request())
    api.after = (stopped, deleted)
    api.workspace_present = False
    recovering = _operations(tmp_path, api, NoopCrashHook())

    # When
    receipt = recovering.recover_workspace_delete()

    # Then
    assert receipt.status == "completed"
    assert receipt.steps[0].status == "verified"
    assert receipt.steps[0].submitted_delete_build_id == deleted.id


@pytest.mark.parametrize("point", ("before_journal", "before_checkpoint"))
def test_delete_crash_boundary_before_every_journal_and_checkpoint(
    tmp_path: Path, point: str
) -> None:
    # Given
    stopped = _build(CoderId("44444444-4444-4444-4444-444444444444"), 1, "stop", "stopped")
    api = FakeMigrationApi((stopped,), (stopped,))
    operations = _operations(tmp_path, api, CrashOnSecondCheckpoint(point))

    # When / Then
    with pytest.raises(SystemExit):
        operations.delete_workspace(_request())
    receipt = CoderMigrationReceiptStore(tmp_path, token_factory=lambda: "unused").load()
    assert receipt is not None
    assert receipt.steps[0].status == "intent"
    assert api.delete_calls == 1
