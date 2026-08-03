from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import (
    CoderApiError,
    CoderBuild,
    CoderBuildStatus,
    CoderId,
    CoderTemplate,
    CoderWorkspace,
)
from dokploy_wizard.dokploy.coder_migration_workspace_models import (
    CoderMigrationBlockedError,
)
from dokploy_wizard.dokploy.coder_template_migration import TemplateMigrationTarget
from tests.unit import coder_template_migration_test_support as migration_support

CrashBeforeSubmissionJournal = migration_support.CrashBeforeSubmissionJournal
_CrashOnce = migration_support.CrashOnce
_OPENWORK_ID = migration_support.OPENWORK_ID
_ORGANIZATION_ID = migration_support.ORGANIZATION_ID
_PI_WEB_ID = migration_support.PI_WEB_ID
_PRIMARY_ID = migration_support.PRIMARY_ID
_WORKSPACE_ID = migration_support.WORKSPACE_ID
_build = migration_support.build
_migration = migration_support.migration
_targets = migration_support.targets


class _Api:
    def __init__(
        self,
        *,
        retired_status: CoderBuildStatus = "stopped",
        unreceipted_template_404: bool = False,
        unreceipted_workspace_404: bool = False,
    ) -> None:
        primary = CoderTemplate(_PRIMARY_ID, _ORGANIZATION_ID, "ubuntu-vscode")
        openwork = CoderTemplate(_OPENWORK_ID, _ORGANIZATION_ID, "ubuntu-vscode-openwork")
        pi_web = CoderTemplate(_PI_WEB_ID, _ORGANIZATION_ID, "ubuntu-vscode-pi-web")
        self.templates = [
            primary,
            CoderTemplate(
                CoderId("66666666-6666-6666-6666-666666666666"),
                _ORGANIZATION_ID,
                "ubuntu-vscode-opencode-web",
            ),
            CoderTemplate(
                CoderId("77777777-7777-7777-7777-777777777777"),
                _ORGANIZATION_ID,
                "ubuntu-vscode-hermes",
            ),
            CoderTemplate(
                CoderId("88888888-8888-8888-8888-888888888888"),
                _ORGANIZATION_ID,
                "ubuntu-vscode-kdense-byok",
            ),
            openwork,
            pi_web,
        ]
        build = _build(CoderId("99999999-9999-9999-9999-999999999999"), 1, "stop", retired_status)
        self.workspaces = [
            CoderWorkspace(_WORKSPACE_ID, openwork.id, "retired-stopped", build),
        ]
        self.builds = {_WORKSPACE_ID: [build]}
        self.rename_calls = 0
        self.delete_workspace_calls = 0
        self.delete_template_calls = 0
        self.unreceipted_template_404 = unreceipted_template_404
        self.unreceipted_workspace_404 = unreceipted_workspace_404

    def default_organization_id(self) -> str:
        return str(_ORGANIZATION_ID)

    def list_templates(self, organization_id: str) -> tuple[CoderTemplate, ...]:
        assert organization_id == _ORGANIZATION_ID
        return tuple(self.templates)

    def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        return tuple(self.workspaces)

    def list_workspace_builds(self, workspace_id: str) -> tuple[CoderBuild, ...]:
        return tuple(self.builds[CoderId(workspace_id)])

    def rename_template(self, template_id: str, name: str) -> CoderTemplate:
        self.rename_calls += 1
        template = next(item for item in self.templates if item.id == template_id)
        renamed = CoderTemplate(template.id, template.organization_id, name)
        self.templates[self.templates.index(template)] = renamed
        return renamed

    def submit_workspace_delete(self, workspace_id: str) -> CoderBuild:
        self.delete_workspace_calls += 1
        workspace = next(item for item in self.workspaces if item.id == workspace_id)
        deleted = _build(CoderId("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), 2, "delete", "deleted")
        self.builds[workspace.id].append(deleted)
        self.workspaces.remove(workspace)
        if self.unreceipted_workspace_404:
            raise CoderApiError(404, "not found", None, ())
        return deleted

    def delete_template(self, template_id: str) -> bytes:
        self.delete_template_calls += 1
        self.templates = [item for item in self.templates if item.id != template_id]
        if self.unreceipted_template_404:
            raise CoderApiError(404, "not found", None, ())
        return b""


class _Pusher:
    def __init__(self, api: _Api | None = None) -> None:
        self._api = api
        self.active_versions = {
            "ubuntu-vscode": "legacy-primary",
            "ubuntu-vscode-opencode-web": "legacy-web",
            "ubuntu-vscode-hermes": "legacy-hermes",
            "ubuntu-vscode-kdense-byok": "legacy-kdense",
        }
        self.versions = {name: (version,) for name, version in self.active_versions.items()}
        self.calls: list[str] = []

    def active_version_name(self, template_name: str) -> str | None:
        if (
            template_name == "ubuntu-vscode-opencode-pi"
            and template_name not in self.active_versions
        ):
            return self.active_versions.get("ubuntu-vscode")
        return self.active_versions.get(template_name)

    def version_names(self, template_name: str) -> tuple[str, ...]:
        if template_name == "ubuntu-vscode-opencode-pi" and template_name not in self.versions:
            return self.versions.get("ubuntu-vscode", ())
        return self.versions.get(template_name, ())

    def push(self, target: TemplateMigrationTarget) -> None:
        self.calls.append(target.name)
        if self._api is not None and not any(
            template.name == target.name for template in self._api.templates
        ):
            self._api.templates.append(
                CoderTemplate(
                    CoderId("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                    _ORGANIZATION_ID,
                    target.name,
                )
            )
        self.active_versions[target.name] = target.version_name
        self.versions[target.name] = (target.version_name,)


def _retirement_ready(
    resource: Literal["template", "workspace"],
) -> tuple[_Api, _Pusher]:
    api = _Api()
    api.rename_template(str(_PRIMARY_ID), "ubuntu-vscode-opencode-pi")
    pusher = _Pusher(api)
    for target in _targets():
        pusher.active_versions[target.name] = target.version_name
        pusher.versions[target.name] = (target.version_name,)
    api.workspaces = [workspace for workspace in api.workspaces if resource == "workspace"]
    return api, pusher


def test_template_migration_happy_preserves_primary_uuid_and_removes_only_retired_assets(
    tmp_path: Path,
) -> None:
    # Given
    api = _Api()
    pusher = _Pusher()

    # When
    receipt = _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())

    # Then
    assert receipt.status == "completed"
    assert {item.name for item in api.templates} == {target.name for target in _targets()}
    primary = next(
        item for item in api.templates if item.name == "ubuntu-vscode-opencode-pi"
    )
    assert primary.id == _PRIMARY_ID
    assert pusher.calls == [target.name for target in _targets()]
    assert api.delete_workspace_calls == 1
    assert api.delete_template_calls == 2


def test_template_migration_blocks_running_retired_workspace_before_any_mutation(
    tmp_path: Path,
) -> None:
    # Given
    api = _Api(retired_status="running")
    pusher = _Pusher()

    # When / Then
    with pytest.raises(CoderMigrationBlockedError, match="exactly stopped") as captured:
        _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())
    assert captured.value.code == "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
    assert pusher.calls == []
    assert api.rename_calls == 0
    assert api.delete_workspace_calls == 0
    assert api.delete_template_calls == 0


def test_existing_push_intent_recovery_accepts_one_active_intended_digest(
    tmp_path: Path,
) -> None:
    # Given
    api = _Api()
    api.rename_template(str(_PRIMARY_ID), "ubuntu-vscode-opencode-pi")
    api.rename_calls = 0
    pusher = _Pusher(api)

    # When
    with pytest.raises(RuntimeError, match="injected migration crash"):
        _migration(tmp_path, api, pusher, _CrashOnce("after_response")).run(
            str(_ORGANIZATION_ID), _targets()
        )
    receipt = _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())

    # Then
    assert receipt.status == "completed"
    assert pusher.calls.count("ubuntu-vscode-opencode-pi") == 1
    assert api.rename_calls == 0


def test_absent_push_intent_recovery_accepts_one_active_intended_digest(
    tmp_path: Path,
) -> None:
    # Given
    api = _Api()
    api.templates = [template for template in api.templates if template.id != _PRIMARY_ID]
    pusher = _Pusher(api)
    del pusher.active_versions["ubuntu-vscode"]
    del pusher.versions["ubuntu-vscode"]

    # When
    with pytest.raises(RuntimeError, match="injected migration crash"):
        _migration(tmp_path, api, pusher, _CrashOnce("after_response")).run(
            str(_ORGANIZATION_ID), _targets()
        )
    receipt = _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())

    # Then
    assert receipt.status == "completed"
    assert pusher.calls.count("ubuntu-vscode-opencode-pi") == 1
    primary = tuple(
        template for template in api.templates if template.name == "ubuntu-vscode-opencode-pi"
    )
    assert len(primary) == 1


@pytest.mark.parametrize("resource", ["template", "workspace"])
def test_unreceipted_404_fails_closed(
    tmp_path: Path, resource: Literal["template", "workspace"]
) -> None:
    api = _Api(
        unreceipted_template_404=resource == "template",
        unreceipted_workspace_404=resource == "workspace",
    )

    with pytest.raises(RuntimeError, match="unreceipted"):
        _migration(tmp_path, api, _Pusher()).run(str(_ORGANIZATION_ID), _targets())

    receipt = CoderMigrationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.status == "blocked"


@pytest.mark.parametrize("resource", ["template", "workspace"])
def test_accepted_delete_response_crash_resumes(
    tmp_path: Path, resource: Literal["template", "workspace"]
) -> None:
    api, pusher = _retirement_ready(resource)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        _migration(tmp_path, api, pusher, _CrashOnce("after_response")).run(
            str(_ORGANIZATION_ID), _targets()
        )
    interim = CoderMigrationReceiptStore(tmp_path).load()
    assert interim is not None
    submitted = tuple(step for step in interim.steps if step.status == "submitted")
    assert len(submitted) == 1
    assert submitted[0].kind == f"delete_{resource}"
    assert submitted[0].request_sha256 is not None
    assert submitted[0].response_sha256 is not None

    receipt = _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())
    assert receipt.status == "completed"


@pytest.mark.parametrize("resource", ["template", "workspace"])
def test_injected_precheckpoint_delete_crash_blocks_unreceipted_disappearance(
    tmp_path: Path, resource: Literal["template", "workspace"]
) -> None:
    api, pusher = _retirement_ready(resource)

    def mutation_count() -> int:
        return api.delete_template_calls + api.delete_workspace_calls

    with pytest.raises(RuntimeError, match="injected pre-checkpoint crash"):
        _migration(
            tmp_path, api, pusher, CrashBeforeSubmissionJournal(mutation_count)
        ).run(str(_ORGANIZATION_ID), _targets())
    with pytest.raises(RuntimeError, match="unreceipted"):
        _migration(tmp_path, api, pusher).run(str(_ORGANIZATION_ID), _targets())

    receipt = CoderMigrationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.status == "blocked"
