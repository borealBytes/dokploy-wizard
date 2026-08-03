from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from dokploy_wizard.dokploy.coder_migration_inventory import CoderInventoryApi
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import CoderBuild

CrashPoint = Literal[
    "before_request",
    "after_response",
    "before_journal",
    "before_checkpoint",
    "after_submit_journal",
    "before_poll",
    "after_poll",
    "before_verified_checkpoint",
]


class CoderMigrationApi(CoderInventoryApi, Protocol):
    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild: ...


class CoderMigrationCrashHook(Protocol):
    def hit(self, point: CrashPoint) -> None: ...


@dataclass(frozen=True, slots=True)
class CoderMigrationPolling:
    attempts: int
    delay_seconds: float
    sleeper: Callable[[float], None]

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.delay_seconds < 0:
            raise ValueError("workspace deletion polling configuration is invalid")


def default_coder_migration_polling() -> CoderMigrationPolling:
    return CoderMigrationPolling(attempts=120, delay_seconds=0.25, sleeper=time.sleep)


@dataclass(frozen=True, slots=True)
class WorkspaceDeletionRequest:
    operation_id: str
    desired_fingerprint: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class CoderMigrationDependencies:
    api: CoderMigrationApi
    receipt_store: CoderMigrationReceiptStore
    clock: Callable[[], str]
    crash_hook: CoderMigrationCrashHook
    polling: CoderMigrationPolling = field(default_factory=default_coder_migration_polling)


class CoderMigrationBlockedError(RuntimeError):
    def __init__(self, reason: str, *, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code

    def __str__(self) -> str:
        return self.reason
