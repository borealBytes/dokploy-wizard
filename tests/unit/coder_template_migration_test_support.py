from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderBuildStatus,
    CoderBuildTransition,
    CoderId,
)
from dokploy_wizard.dokploy.coder_template_migration import (
    CoderTemplateMigration,
    TemplateMigrationDependencies,
    TemplateMigrationTarget,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationApi,
    TemplateMigrationCrashHook,
    TemplateMigrationPusher,
)

ORGANIZATION_ID = CoderId("22222222-2222-2222-2222-222222222222")
PRIMARY_ID = CoderId("11111111-1111-1111-1111-111111111111")
OPENWORK_ID = CoderId("33333333-3333-3333-3333-333333333333")
PI_WEB_ID = CoderId("44444444-4444-4444-4444-444444444444")
WORKSPACE_ID = CoderId("55555555-5555-5555-5555-555555555555")


class InjectedMigrationCrash(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


def build(
    identifier: CoderId,
    number: int,
    transition: CoderBuildTransition,
    status: CoderBuildStatus,
) -> CoderBuild:
    return CoderBuild(identifier, number, transition, status, "2026-07-31T00:00:00Z")


def targets() -> tuple[TemplateMigrationTarget, ...]:
    return tuple(
        TemplateMigrationTarget(
            name=name,
            rendered_sha256=character * 64,
            runtime_lock_sha256="f" * 64,
            version_name=f"dokploy-wizard-{character * 16}",
        )
        for name, character in (
            ("ubuntu-vscode-opencode-pi", "a"),
            ("ubuntu-vscode-opencode-web", "b"),
            ("ubuntu-vscode-hermes", "c"),
            ("ubuntu-vscode-kdense-byok", "d"),
        )
    )


def migration(
    tmp_path: Path,
    api: TemplateMigrationApi,
    pusher: TemplateMigrationPusher,
    crash_hook: TemplateMigrationCrashHook | None = None,
) -> CoderTemplateMigration:
    return CoderTemplateMigration(
        TemplateMigrationDependencies(
            api=api,
            receipt_store=CoderMigrationReceiptStore(
                tmp_path, token_factory=lambda: "receipt-token"
            ),
            pusher=pusher,
            clock=lambda: "2026-07-31T00:00:00Z",
            crash_hook=crash_hook or NoopCrashHook(),
        )
    )


class NoopCrashHook:
    def hit(self, point: str) -> None:
        del point


class CrashOnce:
    def __init__(self, crash_point: str) -> None:
        self._crash_point = crash_point
        self._crashed = False

    def hit(self, point: str) -> None:
        if point == self._crash_point and not self._crashed:
            self._crashed = True
            raise InjectedMigrationCrash("injected migration crash")


class CrashBeforeSubmissionJournal:
    def __init__(self, mutation_count: Callable[[], int]) -> None:
        self._mutation_count = mutation_count
        self._crashed = False

    def hit(self, point: str) -> None:
        if point == "before_journal" and self._mutation_count() and not self._crashed:
            self._crashed = True
            raise InjectedMigrationCrash("injected pre-checkpoint crash")
