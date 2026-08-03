from __future__ import annotations

import hashlib
import json

from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
    JsonValue,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationTarget,
)


def target_fingerprint(targets: tuple[TemplateMigrationTarget, ...]) -> str:
    value: JsonValue = [
        {
            "name": target.name,
            "rendered_sha256": target.rendered_sha256,
            "runtime_lock_sha256": target.runtime_lock_sha256,
            "version_name": target.version_name,
        }
        for target in targets
    ]
    return _sha256(value)


def inventory_sha256(
    templates: tuple[CoderTemplate, ...],
    workspaces: tuple[CoderWorkspace, ...],
    workspace_builds: dict[CoderId, tuple[CoderBuild, ...]],
) -> str:
    template_values: list[JsonValue] = [
        {
            "id": template.id,
            "organization_id": template.organization_id,
            "name": template.name,
        }
        for template in sorted(templates, key=lambda item: str(item.id))
    ]
    workspace_values: list[JsonValue] = [
        {
            "id": workspace.id,
            "template_id": workspace.template_id,
            "name": workspace.name,
            "latest_build": _build_value(workspace.latest_build),
        }
        for workspace in sorted(workspaces, key=lambda item: str(item.id))
    ]
    build_values: dict[str, JsonValue] = {
        str(workspace_id): [_build_value(build) for build in builds]
        for workspace_id, builds in sorted(workspace_builds.items())
    }
    return _sha256(
        {
            "templates": template_values,
            "workspaces": workspace_values,
            "workspace_builds": build_values,
        }
    )


def sha256_text(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


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
