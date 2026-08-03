from __future__ import annotations

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
)
from dokploy_wizard.dokploy.coder_template_migration_fingerprints import sha256_text
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationTarget,
)

_RETAINED_NAMES = (
    "ubuntu-vscode-opencode-pi",
    "ubuntu-vscode-opencode-web",
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-kdense-byok",
)


def validate_targets(targets: tuple[TemplateMigrationTarget, ...]) -> None:
    if tuple(target.name for target in targets) != _RETAINED_NAMES:
        raise CoderMigrationBlockedError("retained Coder template names are not exact")
    for target in targets:
        if not sha256_text(target.rendered_sha256) or not sha256_text(
            target.runtime_lock_sha256
        ):
            raise CoderMigrationBlockedError("template migration digest is invalid")
        if not target.version_name.startswith("dokploy-wizard-"):
            raise CoderMigrationBlockedError("template migration version name is invalid")
    if len({target.version_name for target in targets}) != len(targets):
        raise CoderMigrationBlockedError("template migration versions are ambiguous")


def validate_templates(
    templates: tuple[CoderTemplate, ...], organization_id: CoderId
) -> None:
    if any(template.organization_id != organization_id for template in templates):
        raise CoderMigrationBlockedError("template inventory organization drifted")
    if len({template.id for template in templates}) != len(templates) or len(
        {template.name for template in templates}
    ) != len(templates):
        raise CoderMigrationBlockedError("template inventory is ambiguous")


def validate_workspaces(workspaces: tuple[CoderWorkspace, ...]) -> None:
    if len({workspace.id for workspace in workspaces}) != len(workspaces):
        raise CoderMigrationBlockedError("workspace inventory is ambiguous")


def validate_retired_dependencies(
    workspaces: tuple[CoderWorkspace, ...],
    builds_by_workspace: dict[CoderId, tuple[CoderBuild, ...]],
    retired_template_ids: set[CoderId],
) -> None:
    for workspace in workspaces:
        if workspace.template_id not in retired_template_ids:
            continue
        builds = builds_by_workspace[workspace.id]
        if (
            not builds
            or workspace.latest_build != builds[-1]
            or workspace.latest_build.status != "stopped"
        ):
            raise CoderMigrationBlockedError(
                "retired workspace is not exactly stopped",
                code="CODER_RETIRED_WORKSPACE_NOT_STOPPED",
            )
        if tuple(sorted(builds, key=lambda item: item.build_number)) != builds:
            raise CoderMigrationBlockedError("retired workspace build history is invalid")
