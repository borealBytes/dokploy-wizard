from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from dokploy_wizard.dokploy.coder_migration_receipt_types import (
    MigrationReceipt,
    MigrationStep,
    new_migration_receipt,
    new_migration_step,
)
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderBuild,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationBlockedError
from dokploy_wizard.dokploy.coder_template_migration_fingerprints import (
    inventory_sha256,
    target_fingerprint,
)
from dokploy_wizard.dokploy.coder_template_migration_inventory import (
    RETIRED_TEMPLATE_NAMES,
    read_coder_migration_inventory,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationApi,
    TemplateMigrationPusher,
    TemplateMigrationTarget,
)
from dokploy_wizard.dokploy.coder_template_migration_validation import (
    validate_targets,
)

_LEGACY_PRIMARY_NAME = "ubuntu-vscode"
_PRIMARY_NAME = "ubuntu-vscode-opencode-pi"


def build_migration_receipt(
    api: TemplateMigrationApi,
    pusher: TemplateMigrationPusher,
    organization_id: str,
    targets: tuple[TemplateMigrationTarget, ...],
    created_at: str,
) -> MigrationReceipt:
    validate_targets(targets)
    organization = CoderId(organization_id)
    inventory = read_coder_migration_inventory(api, organization_id)
    templates = inventory.templates
    templates_by_name = {template.name: template for template in templates}
    legacy = templates_by_name.get(_LEGACY_PRIMARY_NAME)
    primary = templates_by_name.get(_PRIMARY_NAME)
    if legacy is not None and primary is not None:
        raise CoderMigrationBlockedError("legacy and target primary template names collide")
    retired = tuple(
        templates_by_name[name]
        for name in sorted(RETIRED_TEMPLATE_NAMES)
        if name in templates_by_name
    )
    workspaces = inventory.workspaces
    workspace_builds = inventory.workspace_builds
    retired_workspace_ids = {template.id for template in retired}
    fingerprint = target_fingerprint(targets)
    inventory_hash = inventory_sha256(templates, workspaces, workspace_builds)
    steps = [
        new_migration_step(
            step_id="inventory", kind="inventory", status="pending", created_at=created_at
        )
    ]
    if legacy is not None:
        steps.append(
            replace(
                new_migration_step(
                    step_id=f"rename-template-{legacy.id}",
                    kind="rename_template",
                    status="intent",
                    created_at=created_at,
                ),
                template_id=legacy.id,
                organization_id=legacy.organization_id,
                name=_PRIMARY_NAME,
                desired_fingerprint=fingerprint,
                pre_inventory_sha256=inventory_hash,
            )
        )
    steps.extend(
        _push_steps(
            targets,
            templates_by_name,
            legacy,
            pusher,
            organization,
            fingerprint,
            inventory_hash,
            created_at,
        )
    )
    steps.extend(
        _workspace_steps(
            workspaces,
            workspace_builds,
            retired_workspace_ids,
            fingerprint,
            inventory_hash,
            created_at,
        )
    )
    steps.extend(_template_steps(retired, fingerprint, inventory_hash, created_at))
    steps.append(
        new_migration_step(step_id="verify", kind="verify", status="pending", created_at=created_at)
    )
    receipt = new_migration_receipt(
        operation_id=str(uuid4()),
        desired_fingerprint=fingerprint,
        pre_inventory_sha256=inventory_hash,
        created_at=created_at,
        step=steps[0],
    )
    return replace(receipt, steps=tuple(steps))


def _push_steps(
    targets: tuple[TemplateMigrationTarget, ...],
    templates_by_name: dict[str, CoderTemplate],
    legacy: CoderTemplate | None,
    pusher: TemplateMigrationPusher,
    organization_id: CoderId,
    fingerprint: str,
    inventory_sha: str,
    created_at: str,
) -> list[MigrationStep]:
    steps: list[MigrationStep] = []
    for target in targets:
        existing = (
            legacy
            if target.name == _PRIMARY_NAME and legacy is not None
            else templates_by_name.get(target.name)
        )
        observed_name = existing.name if existing is not None else target.name
        active_version = pusher.active_version_name(observed_name)
        versions = pusher.version_names(observed_name)
        if len(set(versions)) != len(versions):
            raise CoderMigrationBlockedError("template version inventory is ambiguous")
        if existing is None and (active_version is not None or versions):
            raise CoderMigrationBlockedError("absent template has version inventory")
        if active_version is not None and active_version not in versions:
            raise CoderMigrationBlockedError("active template version is absent from inventory")
        steps.append(
            replace(
                new_migration_step(
                    step_id=f"push-template-{target.name}",
                    kind="push_template",
                    status="intent",
                    created_at=created_at,
                ),
                template_id=None if existing is None else existing.id,
                organization_id=organization_id,
                name=target.name,
                desired_fingerprint=fingerprint,
                pre_inventory_sha256=inventory_sha,
                rendered_sha256=target.rendered_sha256,
                runtime_lock_sha256=target.runtime_lock_sha256,
                template_version_name=target.version_name,
                active_version_name=active_version,
            )
        )
    return steps


def _workspace_steps(
    workspaces: tuple[CoderWorkspace, ...],
    workspace_builds: dict[CoderId, tuple[CoderBuild, ...]],
    retired_template_ids: set[CoderId],
    fingerprint: str,
    inventory_sha: str,
    created_at: str,
) -> list[MigrationStep]:
    return [
        replace(
            new_migration_step(
                step_id=f"delete-workspace-{workspace.id}",
                kind="delete_workspace",
                status="intent",
                created_at=created_at,
            ),
            template_id=workspace.template_id,
            workspace_id=workspace.id,
            name=workspace.name,
            latest_build_id=workspace.latest_build.id,
            latest_build_number=workspace.latest_build.build_number,
            latest_build_status=workspace.latest_build.status,
            pre_delete_build_sequence=workspace_builds[workspace.id],
            stopped=True,
            desired_fingerprint=fingerprint,
            pre_inventory_sha256=inventory_sha,
        )
        for workspace in workspaces
        if workspace.template_id in retired_template_ids
    ]


def _template_steps(
    templates: tuple[CoderTemplate, ...], fingerprint: str, inventory_sha: str, created_at: str
) -> list[MigrationStep]:
    return [
        replace(
            new_migration_step(
                step_id=f"delete-template-{template.id}",
                kind="delete_template",
                status="intent",
                created_at=created_at,
            ),
            template_id=template.id,
            organization_id=template.organization_id,
            dependent_workspace_ids=(),
            dependent_inventory_sha256=inventory_sha,
            desired_fingerprint=fingerprint,
            pre_inventory_sha256=inventory_sha,
        )
        for template in templates
    ]
