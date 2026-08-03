from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderProtocolError,
    CoderTemplate,
    CoderWorkspace,
    JsonValue,
    parse_coder_id,
)


class CoderInventoryApi(Protocol):
    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]: ...

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]: ...

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]: ...


@dataclass(frozen=True, slots=True)
class WorkspaceDeletionSnapshot:
    workspace: CoderWorkspace
    builds: tuple[CoderBuild, ...]
    inventory_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceRecoverySnapshot:
    workspace: CoderWorkspace | None
    builds: tuple[CoderBuild, ...]
    inventory_sha256: str


@dataclass(frozen=True, slots=True)
class TemplateDeletionSnapshot:
    template: CoderTemplate | None
    dependent_workspace_ids: tuple[CoderId, ...]
    inventory_sha256: str
    dependent_inventory_sha256: str


def workspace_deletion_snapshot(
    api: CoderInventoryApi, workspace_id: str
) -> WorkspaceDeletionSnapshot:
    workspaces = api.list_workspaces()
    workspace = _workspace(workspaces, parse_coder_id(workspace_id, "workspace_id"))
    builds = tuple(
        sorted(api.list_workspace_builds(workspace_id), key=lambda build: build.build_number)
    )
    _validate_build_sequence(workspace, builds)
    return WorkspaceDeletionSnapshot(
        workspace=workspace,
        builds=builds,
        inventory_sha256=_sha256(_workspace_inventory_value(workspaces, builds)),
    )


def template_deletion_snapshot(
    api: CoderInventoryApi, organization_id: str, template_id: str
) -> TemplateDeletionSnapshot:
    templates = api.list_templates(organization_id)
    workspaces = api.list_workspaces()
    requested_template = parse_coder_id(template_id, "template_id")
    template = next((item for item in templates if item.id == requested_template), None)
    dependents = tuple(
        sorted(
            (
                workspace.id
                for workspace in workspaces
                if workspace.template_id == requested_template
            ),
            key=str,
        )
    )
    return TemplateDeletionSnapshot(
        template=template,
        dependent_workspace_ids=dependents,
        inventory_sha256=_sha256(_template_inventory_value(templates, workspaces)),
        dependent_inventory_sha256=_sha256(_workspace_value(workspaces)),
    )


def workspace_recovery_snapshot(
    api: CoderInventoryApi, workspace_id: str
) -> WorkspaceRecoverySnapshot:
    workspaces = api.list_workspaces()
    matching = tuple(
        workspace
        for workspace in workspaces
        if workspace.id == parse_coder_id(workspace_id, "workspace_id")
    )
    if len(matching) > 1:
        raise CoderProtocolError("workspace inventory contains duplicate requested UUID")
    builds = tuple(
        sorted(api.list_workspace_builds(workspace_id), key=lambda build: build.build_number)
    )
    _validate_unique_builds(builds)
    workspace = None if not matching else matching[0]
    if workspace is not None:
        if not builds or workspace.latest_build != builds[-1]:
            raise CoderProtocolError(
                "workspace latest build does not match complete build inventory"
            )
    return WorkspaceRecoverySnapshot(
        workspace=workspace,
        builds=builds,
        inventory_sha256=_sha256(_workspace_inventory_value(workspaces, builds)),
    )


def build_sha256(build: CoderBuild) -> str:
    return _sha256(_build_value(build))


def _workspace(workspaces: tuple[CoderWorkspace, ...], workspace_id: CoderId) -> CoderWorkspace:
    matching = tuple(workspace for workspace in workspaces if workspace.id == workspace_id)
    if len(matching) != 1:
        raise CoderProtocolError("workspace inventory must contain exactly one requested UUID")
    return matching[0]


def _validate_build_sequence(workspace: CoderWorkspace, builds: tuple[CoderBuild, ...]) -> None:
    if not builds:
        raise CoderProtocolError("workspace build inventory must not be empty")
    _validate_unique_builds(builds)
    if workspace.latest_build != builds[-1]:
        raise CoderProtocolError("workspace latest build does not match complete build inventory")


def _validate_unique_builds(builds: tuple[CoderBuild, ...]) -> None:
    ids = tuple(build.id for build in builds)
    numbers = tuple(build.build_number for build in builds)
    if len(set(ids)) != len(ids) or len(set(numbers)) != len(numbers):
        raise CoderProtocolError("workspace build inventory contains duplicate identities")


def _workspace_inventory_value(
    workspaces: tuple[CoderWorkspace, ...], builds: tuple[CoderBuild, ...]
) -> JsonValue:
    return {
        "workspaces": _workspace_value(workspaces),
        "builds": [_build_value(build) for build in builds],
    }


def _template_inventory_value(
    templates: tuple[CoderTemplate, ...], workspaces: tuple[CoderWorkspace, ...]
) -> JsonValue:
    return {
        "templates": [
            {
                "id": template.id,
                "organization_id": template.organization_id,
                "name": template.name,
            }
            for template in sorted(templates, key=lambda template: str(template.id))
        ],
        "workspaces": _workspace_value(workspaces),
    }


def _workspace_value(workspaces: tuple[CoderWorkspace, ...]) -> list[JsonValue]:
    return [
        {
            "id": workspace.id,
            "template_id": workspace.template_id,
            "name": workspace.name,
            "latest_build": _build_value(workspace.latest_build),
        }
        for workspace in sorted(workspaces, key=lambda workspace: str(workspace.id))
    ]


def _build_value(build: CoderBuild) -> dict[str, JsonValue]:
    return {
        "id": build.id,
        "build_number": build.build_number,
        "transition": build.transition,
        "status": build.status,
        "created_at": build.created_at,
    }


def _sha256(value: JsonValue) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
