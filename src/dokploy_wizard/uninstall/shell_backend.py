"""Process-backed default backend for ownership-ledger teardown."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dokploy_wizard.dokploy.shared_core_schedule import disable_owned_schedule_contract
from dokploy_wizard.dokploy.sync_helper_runtime import SubprocessDockerHelperRuntime
from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    RawEnvInput,
    load_state_dir,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncDesiredState,
)
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.uninstall.cloudflare_teardown import CloudflareUninstallTeardown
from dokploy_wizard.uninstall.docker_teardown import DockerUninstallTeardown
from dokploy_wizard.uninstall.dokploy_teardown import DokployUninstallTeardown
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.families import ProviderFamily, family_for
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import UninstallProviderClients
from dokploy_wizard.uninstall.sync_quiescence import (
    SyncScheduleQuiescence,
    quiesce_sync_schedule,
)
from dokploy_wizard.uninstall.sync_schedule import SyncScheduleTeardown
from dokploy_wizard.uninstall.tailscale_teardown import TailscaleUninstallTeardown


class ShellUninstallBackend:
    """Default process-backed backend for receipt-authorized uninstall mutations."""

    def __init__(
        self,
        raw_input: RawEnvInput,
        *,
        state_dir: Path | None = None,
        api_url: str | None = None,
        providers: UninstallProviderClients | None = None,
    ) -> None:
        values = raw_input.values
        self._values = values
        self._raw_input = raw_input
        self._state_dir = state_dir
        self._api_url = api_url
        self._providers = providers or UninstallProviderClients()
        self._cloudflare_teardown = CloudflareUninstallTeardown(
            raw_input=raw_input,
            state_dir=state_dir,
            providers=self._providers,
        )
        self._tailscale_teardown = TailscaleUninstallTeardown(
            raw_input=raw_input,
            state_dir=state_dir,
            providers=self._providers,
        )
        self._dokploy_teardown = DokployUninstallTeardown(
            raw_input=raw_input,
            state_dir=state_dir,
            providers=self._providers,
            deleted_targets=set(),
        )
        self._docker_teardown = DockerUninstallTeardown(
            state_dir=state_dir,
            providers=self._providers,
            deleted_targets=set(),
        )
        self._failing_types = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_TYPES", "").split(",")
            if item.strip() != ""
        }
        self._failing_ids = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_IDS", "").split(",")
            if item.strip() != ""
        }

    def delete(self, deletion: PlannedDeletion) -> None:
        if deletion.resource.resource_type in self._failing_types:
            raise UninstallExecutionError(
                "Simulated uninstall failure for resource type "
                f"'{deletion.resource.resource_type}'."
            )
        if deletion.resource.resource_id in self._failing_ids:
            raise UninstallExecutionError(
                f"Simulated uninstall failure for resource id '{deletion.resource.resource_id}'."
            )
        try:
            family = family_for(deletion.resource.resource_type)
        except RuntimeError as error:
            raise UninstallExecutionError(str(error)) from error
        match family:
            case ProviderFamily.SCHEDULE:
                if self._state_dir is None:
                    raise UninstallExecutionError("Sync schedule teardown requires state.")
                with quiesce_sync_schedule(self._quiescence_from_state(deletion.resource)):
                    self._sync_teardown().delete(deletion.resource)
                return
            case ProviderFamily.TAILSCALE:
                self._tailscale_teardown.delete(deletion)
                return
            case ProviderFamily.CLOUDFLARE:
                self._cloudflare_teardown.delete(deletion)
                return
            case ProviderFamily.DOKPLOY:
                self._dokploy_teardown.delete(deletion)
                return
            case ProviderFamily.DOCKER:
                self._docker_teardown.delete(deletion)
                return
        raise UninstallExecutionError(
            "Owned resource has no production deletion adapter: "
            f"'{deletion.resource.resource_type}'."
        )

    def destroy_coder_secrets(self, *, desired_state: DesiredState) -> None:
        from dokploy_wizard.uninstall.coder_secrets import CoderSecretUninstaller

        if self._state_dir is None:
            raise UninstallExecutionError("Coder secret destroy requires state.")
        CoderSecretUninstaller(self._values, self._state_dir).destroy(desired_state)

    def disable_sync_schedule(
        self,
        *,
        resource: OwnedResource,
        desired: SyncDesiredState,
        applied: AppliedSyncState,
    ) -> AppliedSyncState:
        metadata = resource.metadata
        if metadata is None or metadata.owner_id != desired.owner_id:
            raise UninstallExecutionError("Retained sync schedule ownership is invalid.")
        with quiesce_sync_schedule(self._quiescence_for(resource, desired, applied)):
            result = disable_owned_schedule_contract(
                client=self._sync_teardown().client(),
                desired=desired,
                applied=applied,
            )
        if result.tombstone is None or self._state_dir is None:
            raise UninstallExecutionError("Retained sync schedule produced no tombstone.")
        digest = SyncArtifactStore(self._state_dir).persist_disable_tombstone(result.tombstone)
        return replace(result.applied, disable_tombstone_sha256=digest)

    def _sync_teardown(self) -> SyncScheduleTeardown:
        if self._state_dir is None:
            raise UninstallExecutionError("Sync schedule teardown requires state.")
        return SyncScheduleTeardown(self._values, self._state_dir, self._api_url)

    def _quiescence_from_state(self, resource: OwnedResource) -> SyncScheduleQuiescence:
        if self._state_dir is None:
            raise UninstallExecutionError("Sync schedule teardown requires state.")
        loaded = load_state_dir(self._state_dir)
        if loaded.desired_state is None or loaded.applied_state is None:
            raise UninstallExecutionError("Sync schedule teardown requires complete state.")
        desired = loaded.desired_state.opencode_go_sync
        applied = loaded.applied_state.opencode_go_sync
        if desired is None or applied is None:
            raise UninstallExecutionError("Sync schedule teardown requires sync state.")
        return self._quiescence_for(resource, desired, applied)

    def _quiescence_for(
        self,
        resource: OwnedResource,
        desired: SyncDesiredState,
        applied: AppliedSyncState,
    ) -> SyncScheduleQuiescence:
        metadata_root = SubprocessDockerHelperRuntime().volume_mountpoint(desired.metadata_volume)
        return SyncScheduleQuiescence(
            metadata_root=metadata_root,
            resource=resource,
            desired=desired,
            applied=applied,
        )
