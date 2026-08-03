"""Authenticated Coder operations used by the Host A upgrade proof."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

from dokploy_wizard.dokploy.coder_migration_api import CoderMigrationApi
from dokploy_wizard.dokploy.coder_migration_types import CoderWorkspace
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import HostAObservation
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    ManagedHostSnapshot,
    RetiredFixtureEvidence,
    UpgradeHostAError,
)
from dokploy_wizard.proof.workspace_proof_receipts import WorkspaceProofRef

_RUNNING_FIXTURE = "dokploy-wizard-upgrade-running"
_STOPPED_FIXTURE = "dokploy-wizard-upgrade-stopped"
_RUNNING_RETIRED_TEMPLATE = "ubuntu-vscode-openwork"
_STOPPED_RETIRED_TEMPLATE = "ubuntu-vscode-pi-web"
_PRIMARY_NAMES = frozenset(("ubuntu-vscode", "ubuntu-vscode-opencode-pi"))


class CoderUpgradeClient:
    """Resumable Coder fixtures, inventory, and proof-workspace lifecycle adapter."""

    def __init__(
        self,
        api: CoderMigrationApi,
        organization_id: str,
        workspace_test: Callable[[str, str], None],
    ) -> None:
        self._api = api
        self._organization_id = organization_id
        self._workspace_test = workspace_test

    def snapshot(self, observation: HostAObservation | None) -> ManagedHostSnapshot:
        primary_uuid, names, retained_state_sha256 = self._inventory()
        return ManagedHostSnapshot(
            primary_uuid=primary_uuid,
            template_names=names,
            retained_state_sha256=retained_state_sha256,
            catalog_exact=observation is not None and observation.catalog_exact,
            schedule_exact=observation is not None and observation.schedule_exact,
            source_exact=observation is not None and observation.source_exact,
        )

    def destructive_state_sha256(self) -> str:
        return self._inventory()[2]

    def _inventory(self) -> tuple[str, tuple[str, ...], str]:
        templates = self._api.list_templates(self._organization_id)
        primary = tuple(template for template in templates if template.name in _PRIMARY_NAMES)
        if len(primary) != 1:
            raise UpgradeHostAError("Coder primary template identity is ambiguous")
        workspaces = self._api.list_workspaces()
        inventory = {
            "templates": sorted((str(item.id), item.name) for item in templates),
            "workspaces": sorted(
                (
                    str(item.id),
                    item.name,
                    str(item.template_id),
                    str(item.latest_build.id),
                    item.latest_build.build_number,
                    item.latest_build.status,
                    item.latest_build.transition,
                )
                for item in workspaces
            ),
            "builds": sorted(
                (
                    str(workspace.id),
                    tuple(
                        (
                            str(build.id),
                            build.build_number,
                            build.status,
                            build.transition,
                        )
                        for build in self._api.list_workspace_builds(str(workspace.id))
                    ),
                )
                for workspace in workspaces
            ),
        }
        names = tuple(sorted(template.name for template in templates))
        return (
            str(primary[0].id),
            names,
            hashlib.sha256(
                json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def create_retired_fixtures(self) -> RetiredFixtureEvidence:
        templates = {item.name: item for item in self._api.list_templates(self._organization_id)}
        running_template = templates.get(_RUNNING_RETIRED_TEMPLATE)
        stopped_template = templates.get(_STOPPED_RETIRED_TEMPLATE)
        if running_template is None or stopped_template is None:
            raise UpgradeHostAError("Retired Coder fixture templates are unavailable")
        running = self._ensure_workspace(
            _RUNNING_FIXTURE,
            str(running_template.id),
            target_status="running",
        )
        stopped = self._ensure_workspace(
            _STOPPED_FIXTURE,
            str(stopped_template.id),
            target_status="stopped",
        )
        return RetiredFixtureEvidence(
            running_workspace_id=str(running.id),
            running_workspace_name=running.name,
            running_template_id=str(running_template.id),
            running_template_name=running_template.name,
            stopped_workspace_id=str(stopped.id),
            stopped_workspace_name=stopped.name,
            stopped_template_id=str(stopped_template.id),
            stopped_template_name=stopped_template.name,
        )

    def stop_running_fixture(self, workspace_id: str) -> None:
        workspace = self._workspace_by_id(workspace_id)
        if workspace.latest_build.status != "stopped":
            self._api.submit_workspace_transition(workspace_id, "stop")
            self._wait_for_status(workspace_id, "stopped")

    def find(self, workspace_name: str) -> tuple[WorkspaceProofRef, ...]:
        templates = {
            str(item.id): item.name for item in self._api.list_templates(self._organization_id)
        }
        return tuple(
            WorkspaceProofRef(str(item.id), item.name, templates[str(item.template_id)])
            for item in self._api.list_workspaces()
            if item.name == workspace_name and str(item.template_id) in templates
        )

    def create(self, *, template_name: str, workspace_name: str) -> WorkspaceProofRef:
        templates = {
            item.name: str(item.id) for item in self._api.list_templates(self._organization_id)
        }
        template_id = templates.get(template_name)
        if template_id is None:
            raise UpgradeHostAError("Proof workspace template is unavailable")
        workspace = self._api.create_workspace(
            template_id=template_id,
            workspace_name=workspace_name,
        )
        running = self._wait_for_status(str(workspace.id), "running")
        return WorkspaceProofRef(str(running.id), running.name, template_name)

    def test(self, workspace: WorkspaceProofRef) -> None:
        running = self._workspace_by_id(workspace.workspace_id)
        if running.latest_build.status != "running":
            raise UpgradeHostAError("Proof workspace is not running")
        self._workspace_test(workspace.template_name, workspace.workspace_name)

    def stop(self, workspace: WorkspaceProofRef) -> None:
        self.stop_running_fixture(workspace.workspace_id)

    def delete(self, workspace: WorkspaceProofRef) -> None:
        matches = tuple(
            item for item in self._api.list_workspaces() if str(item.id) == workspace.workspace_id
        )
        if not matches:
            return
        if len(matches) != 1 or matches[0].latest_build.status != "stopped":
            raise UpgradeHostAError("Proof workspace deletion requires one stopped workspace")
        self._api.submit_workspace_delete(workspace.workspace_id)
        self._wait_for_absence(workspace.workspace_id)

    def _ensure_workspace(
        self,
        workspace_name: str,
        template_id: str,
        *,
        target_status: str,
    ) -> CoderWorkspace:
        matches = tuple(item for item in self._api.list_workspaces() if item.name == workspace_name)
        if len(matches) > 1 or (matches and str(matches[0].template_id) != template_id):
            raise UpgradeHostAError("Retired Coder fixture identity drifted")
        workspace = (
            matches[0]
            if matches
            else self._api.create_workspace(template_id=template_id, workspace_name=workspace_name)
        )
        workspace = self._wait_for_terminal_start(str(workspace.id))
        if target_status == "stopped" and workspace.latest_build.status != "stopped":
            self._api.submit_workspace_transition(str(workspace.id), "stop")
            return self._wait_for_status(str(workspace.id), "stopped")
        if target_status == "running" and workspace.latest_build.status == "stopped":
            self._api.submit_workspace_transition(str(workspace.id), "start")
            return self._wait_for_status(str(workspace.id), "running")
        return workspace

    def _wait_for_terminal_start(self, workspace_id: str) -> CoderWorkspace:
        workspace = self._workspace_by_id(workspace_id)
        if workspace.latest_build.status in {"running", "stopped"}:
            return workspace
        return self._wait_for_status(workspace_id, "running")

    def _wait_for_status(self, workspace_id: str, status: str) -> CoderWorkspace:
        for _attempt in range(240):
            workspace = self._workspace_by_id(workspace_id)
            if workspace.latest_build.status == status:
                return workspace
            if workspace.latest_build.status in {"failed", "canceled", "deleted"}:
                raise UpgradeHostAError("Coder workspace build reached a terminal failure")
            time.sleep(1)
        raise UpgradeHostAError("Coder workspace build status deadline expired")

    def _wait_for_absence(self, workspace_id: str) -> None:
        for _attempt in range(240):
            if not any(str(item.id) == workspace_id for item in self._api.list_workspaces()):
                return
            time.sleep(1)
        raise UpgradeHostAError("Coder workspace deletion deadline expired")

    def _workspace_by_id(self, workspace_id: str) -> CoderWorkspace:
        matches = tuple(
            item for item in self._api.list_workspaces() if str(item.id) == workspace_id
        )
        if len(matches) != 1:
            raise UpgradeHostAError("Coder workspace identity is absent or ambiguous")
        return matches[0]
