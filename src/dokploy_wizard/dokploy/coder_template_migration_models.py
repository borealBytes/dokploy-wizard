from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderTemplate,
    CoderWorkspace,
)
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationPolling,
    default_coder_migration_polling,
)


@dataclass(frozen=True, slots=True)
class TemplateMigrationTarget:
    name: str
    rendered_sha256: str
    runtime_lock_sha256: str
    version_name: str


class TemplateMigrationApi(Protocol):
    def default_organization_id(self) -> str: ...

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]: ...

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]: ...

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]: ...

    def rename_template(self, template_id: str, name: str) -> CoderTemplate: ...

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild: ...

    def delete_template(self, template_id: str) -> bytes: ...


class TemplateMigrationPusher(Protocol):
    def active_version_name(self, template_name: str) -> str | None: ...

    def version_names(self, template_name: str) -> tuple[str, ...]: ...

    def push(self, target: TemplateMigrationTarget) -> None: ...


class TemplateMigrationCrashHook(Protocol):
    def hit(self, point: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TemplateMigrationDependencies:
    api: TemplateMigrationApi
    receipt_store: CoderMigrationReceiptStore
    pusher: TemplateMigrationPusher
    clock: Callable[[], str]
    crash_hook: TemplateMigrationCrashHook
    polling: CoderMigrationPolling = field(default_factory=default_coder_migration_polling)
