from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from dokploy_wizard.dokploy.coder_migration_inventory import CoderInventoryApi
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationCrashHook


class CoderTemplateMigrationApi(CoderInventoryApi, Protocol):
    def delete_template(self, template_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TemplateDeletionRequest:
    operation_id: str
    desired_fingerprint: str
    organization_id: str
    template_id: str


@dataclass(frozen=True, slots=True)
class TemplateDeletionDependencies:
    api: CoderTemplateMigrationApi
    receipt_store: CoderMigrationReceiptStore
    clock: Callable[[], str]
    crash_hook: CoderMigrationCrashHook
