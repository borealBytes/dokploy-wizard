"""Read-only Coder migration inventory shared by preflight and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)
from dokploy_wizard.dokploy.coder_template_migration_models import TemplateMigrationApi
from dokploy_wizard.dokploy.coder_template_migration_validation import (
    validate_retired_dependencies,
    validate_templates,
    validate_workspaces,
)

RETIRED_TEMPLATE_NAMES: Final = frozenset(("ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"))


@dataclass(frozen=True, slots=True)
class CoderMigrationInventory:
    templates: tuple[CoderTemplate, ...]
    workspaces: tuple[CoderWorkspace, ...]
    workspace_builds: dict[CoderId, tuple[CoderBuild, ...]]


def read_coder_migration_inventory(
    api: TemplateMigrationApi,
    organization_id: str,
) -> CoderMigrationInventory:
    """Read and validate all retired-template dependencies without mutation."""

    organization = CoderId(organization_id)
    templates = api.list_templates(organization)
    validate_templates(templates, organization)
    retired_template_ids = {
        template.id for template in templates if template.name in RETIRED_TEMPLATE_NAMES
    }
    workspaces = api.list_workspaces()
    validate_workspaces(workspaces)
    workspace_builds = {
        workspace.id: tuple(api.list_workspace_builds(workspace.id))
        for workspace in workspaces
        if workspace.template_id in retired_template_ids
    }
    validate_retired_dependencies(workspaces, workspace_builds, retired_template_ids)
    return CoderMigrationInventory(templates, workspaces, workspace_builds)
