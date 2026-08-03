from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.lifecycle.changes import applicable_phases_for
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.dokploy_runtime_auth import (
    DokployRuntimeAuth,
    persist_dokploy_runtime_auth,
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
)
from dokploy_wizard.uninstall.executor import UninstallBackend, execute_uninstall_plan
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan
from dokploy_wizard.uninstall.result import UninstallExecutionResult

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


def _sync_desired(stack_name: str) -> SyncDesiredState:
    return SyncDesiredState.from_schedule(
        owner_id=_OWNER,
        config_sha256="a" * 64,
        litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
        metadata_volume=f"{stack_name}-shared-litellm-data",
        schedule_spec=ScheduleSpec.for_shared_core(
            stack_name=stack_name,
            compose_id="compose-1",
            owner_id=_OWNER,
        ),
    )


def _sync_resource() -> OwnedResource:
    return OwnedResource(
        resource_type="shared_core_sync_schedule",
        resource_id="schedule-1",
        scope="shared-core-sync:wizard",
        metadata=SyncOwnershipMetadata(
            owner_id=_OWNER,
            action_provenance="created",
            remote_fingerprint="c" * 64,
            spec_hash="d" * 64,
            physical_target_id="schedule-1",
            creation_receipt_sha256="e" * 64,
            preimage_receipt_sha256=None,
            deletion_policy="delete",
        ),
    )


@dataclass(slots=True)  # noqa: MUTABLE_OK - records retained schedule operations
class RetainingBackend:
    tombstone: str = "f" * 64
    disabled: list[str] = field(default_factory=list)

    def delete(self, deletion: PlannedDeletion) -> None:
        raise AssertionError(f"unexpected deletion: {deletion.resource.resource_id}")

    def disable_sync_schedule(
        self,
        *,
        resource: OwnedResource,
        desired: SyncDesiredState,
        applied: AppliedSyncState,
    ) -> AppliedSyncState:
        assert desired.owner_id == _OWNER
        self.disabled.append(resource.resource_id)
        return replace(applied, disable_tombstone_sha256=self.tombstone)


def test_retain_without_deletions_checkpoints_disabled_sync_state(tmp_path: Path) -> None:
    raw = parse_env_file(_FIXTURE)
    base = resolve_desired_state(raw)
    sync_desired = _sync_desired(base.stack_name)
    desired = replace(base, opencode_go_sync=sync_desired)
    resource = _sync_resource()
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=("shared_core",),
            runtime_images=desired.runtime_images,
            opencode_go_sync=AppliedSyncState.initial(sync_desired),
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    backend = RetainingBackend()
    plan = UninstallPlan(
        mode="retain",
        environment=desired.stack_name,
        deletions=(),
        retained_resources=(resource,),
        warnings=(),
    )

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    loaded = load_state_dir(tmp_path)
    assert backend.disabled == ["schedule-1"]
    assert loaded.applied_state is not None
    assert loaded.applied_state.opencode_go_sync is not None
    assert loaded.applied_state.opencode_go_sync.disable_tombstone_sha256 == backend.tombstone


def test_production_uninstall_uses_runtime_auth_without_rewriting_raw_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = parse_env_file(_FIXTURE)
    raw = RawEnvInput(
        format_version=source.format_version,
        values={
            key: value
            for key, value in source.values.items()
            if key not in {"DOKPLOY_API_KEY", "DOKPLOY_API_URL"}
        },
    )
    desired = resolve_desired_state(raw)
    ledger = OwnershipLedger(format_version=1, resources=())
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=applicable_phases_for(desired),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    raw_bytes = (tmp_path / "raw-input.json").read_bytes()
    persist_dokploy_runtime_auth(
        tmp_path,
        DokployRuntimeAuth(
            api_url="https://runtime.example.com/api",
            api_key="runtime-only-key",
        ),
    )
    received: dict[str, str] = {}

    @dataclass(frozen=True, slots=True)
    class RecordingShellBackend:
        raw_input: RawEnvInput
        state_dir: Path | None = None
        api_url: str | None = None

        def __post_init__(self) -> None:
            received.update(self.raw_input.values)

        def delete(self, deletion: PlannedDeletion) -> None:
            raise AssertionError(f"unexpected deletion: {deletion.resource.resource_id}")

    def fake_execute(
        *,
        state_dir: Path,
        raw_input: RawEnvInput,
        desired_state: DesiredState,
        ownership_ledger: OwnershipLedger,
        plan: UninstallPlan,
        backend: UninstallBackend,
        dry_run: bool,
    ) -> UninstallExecutionResult:
        del state_dir, raw_input, desired_state, ownership_ledger, plan, backend, dry_run
        return UninstallExecutionResult((), (), False)

    monkeypatch.setattr(cli, "ShellUninstallBackend", RecordingShellBackend)
    monkeypatch.setattr(cli, "execute_uninstall_plan", fake_execute)

    cli._run_uninstall_flow_locked(
        state_dir=tmp_path,
        destroy_data=False,
        dry_run=True,
        non_interactive=True,
        confirm_file=None,
        uninstall_backend=None,
        locked_stack_name=desired.stack_name,
    )

    assert received["DOKPLOY_API_URL"] == "https://runtime.example.com/api"
    assert received["DOKPLOY_API_KEY"] == "runtime-only-key"
    assert (tmp_path / "raw-input.json").read_bytes() == raw_bytes
