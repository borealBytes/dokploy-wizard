# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard.cli import run_modify_flow
from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.shared_core_schedule import (
    disable_owned_schedule,
    reconcile_owned_schedule_contract,
)
from dokploy_wizard.dokploy.shared_core_schedule_receipt import (
    ScheduleMutationReceipt,
    schedule_record_fingerprint,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
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
from dokploy_wizard.state.shared_core_sync import (
    SyncOwnershipMetadata,
    SyncStateError,
    ensure_owner,
)
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall import shell_backend
from dokploy_wizard.uninstall.executor import ShellUninstallBackend
from dokploy_wizard.uninstall.planner import PlannedDeletion

from .task5_schedule_fakes import (
    FOREIGN_OWNER_ID,
    ScheduleClient,
    applied_with_schedule,
)
from .task5_schedule_fakes import (
    desired as task5_desired,
)
from .task5_schedule_fakes import (
    record as task5_record,
)
from .task11_authority_fakes import seed_creation_authority


def test_reused_unowned_write_is_rejected() -> None:
    sync_desired = task5_desired()
    foreign = task5_record(task5_desired(FOREIGN_OWNER_ID))
    client = ScheduleClient(schedules=[foreign])

    with pytest.raises(SyncStateError):
        disable_owned_schedule(
            client=client,
            desired=sync_desired,
            applied=applied_with_schedule(sync_desired, foreign.schedule_id),
        )

    assert client.calls == ["list"]


def test_legacy_provenance_ambiguous_rejects_same_name_schedules() -> None:
    sync_desired = task5_desired()
    client = ScheduleClient(
        schedules=[
            task5_record(sync_desired, schedule_id="sync-1"),
            task5_record(sync_desired, schedule_id="sync-2"),
        ]
    )

    with pytest.raises(SyncStateError, match="Multiple schedules"):
        reconcile_owned_schedule_contract(
            client=client,
            desired=sync_desired,
            applied=applied_with_schedule(sync_desired, "sync-1"),
            existing_metadata=None,
        )

    assert client.calls == ["list"]


def test_disable_rejects_concurrent_schedule_spec_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_desired = task5_desired()
    owned = task5_record(sync_desired)
    client = ScheduleClient(schedules=[owned])
    update = client.update_schedule

    def drifted_update(
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        updated = update(
            schedule_id=schedule_id,
            name=name,
            compose_id=compose_id,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        client.schedules = [replace(updated, command=f"{updated.command} --drift")]
        return client.schedules[0]

    monkeypatch.setattr(client, "update_schedule", drifted_update)

    with pytest.raises(SyncStateError, match="persist"):
        disable_owned_schedule(
            client=client,
            desired=sync_desired,
            applied=applied_with_schedule(sync_desired, owned.schedule_id),
        )

    assert client.calls == ["list", "update", "list"]


def test_disable_quiesces_sync_preserves_shared_core_and_physical_target_coalescing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_desired = task5_desired()
    owned = task5_record(sync_desired)
    receipt = ScheduleMutationReceipt(
        owner_id=sync_desired.owner_id,
        action="created",
        desired_fingerprint=sync_desired.fingerprint(),
        before_spec_sha256=None,
        remote=owned,
    )
    receipt_digest = SyncArtifactStore(tmp_path).persist_schedule_receipt(receipt)
    metadata = SyncOwnershipMetadata(
        owner_id=sync_desired.owner_id,
        action_provenance="created",
        remote_fingerprint=schedule_record_fingerprint(owned),
        spec_hash=sync_desired.schedule_spec_sha256,
        physical_target_id=owned.schedule_id,
        creation_receipt_sha256=receipt_digest,
        preimage_receipt_sha256=None,
        deletion_policy="delete",
    )
    resource = OwnedResource(
        resource_type="shared_core_sync_schedule",
        resource_id=owned.schedule_id,
        scope="shared-core-sync:wizard",
        metadata=metadata,
    )
    metadata_root = tmp_path / "detached-metadata"
    metadata_root.mkdir()
    lkg = metadata_root / "catalog-lkg.json"
    lkg.write_text("lkg", encoding="utf-8")
    ensure_owner(
        tmp_path / "shared-core-sync-owner.json",
        owner_id=sync_desired.owner_id,
    )
    client = ScheduleClient(schedules=[owned])

    class VolumeRuntime:
        def volume_mountpoint(self, volume: str) -> Path:
            assert volume == sync_desired.metadata_volume
            return metadata_root

    class Teardown:
        def client(self) -> ScheduleClient:
            return client

    backend = ShellUninstallBackend(
        RawEnvInput(format_version=1, values={"TEST": "1"}),
        state_dir=tmp_path,
    )
    monkeypatch.setattr(shell_backend, "SubprocessDockerHelperRuntime", VolumeRuntime)
    monkeypatch.setattr(backend, "_sync_teardown", Teardown)

    disabled = backend.disable_sync_schedule(
        resource=resource,
        desired=sync_desired,
        applied=applied_with_schedule(sync_desired, owned.schedule_id),
    )

    tombstone_digest = disabled.disable_tombstone_sha256
    assert tombstone_digest is not None
    assert (metadata_root / "sync.lock").is_file()
    assert lkg.read_text(encoding="utf-8") == "lkg"
    assert (tmp_path / "shared-core-sync-owner.json").is_file()
    assert SyncArtifactStore(tmp_path).load_schedule_receipt(receipt_digest) == receipt
    tombstone = SyncArtifactStore(tmp_path).load_disable_tombstone(tombstone_digest)
    assert tombstone.schedule_id == owned.schedule_id

    resumed = reconcile_owned_schedule_contract(
        client=client,
        desired=sync_desired,
        applied=disabled,
        existing_metadata=metadata,
    )

    assert resumed.applied.dokploy_schedule_id == owned.schedule_id
    assert resumed.metadata.physical_target_id == owned.schedule_id
    assert client.calls == ["list", "update", "list", "list", "update", "list"]


FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _replace_line(content: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return content + f"\n{key}={value}\n"


def _seed_nextcloud_state(state_dir: Path) -> None:
    base_raw_input = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    raw_input = RawEnvInput(
        format_version=base_raw_input.format_version,
        values={
            **base_raw_input.values,
            "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID": "nextcloud-stack-tunnel",
            "CLOUDFLARE_MOCK_EXISTING_HOSTNAMES": (
                "dokploy.example.com,headscale.example.com,nextcloud.example.com,office.example.com"
            ),
            "HEADSCALE_MOCK_EXISTING_SERVICE_ID": "nextcloud-stack-headscale",
        },
    )
    desired_state = resolve_desired_state(raw_input)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "headscale",
                "nextcloud",
            ),
        ),
    )
    ledger = OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource("cloudflare_tunnel", "nextcloud-stack-tunnel", "account:account-123"),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dokploy-example-com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "headscale-example-com",
                    "zone:zone-123:headscale.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "nextcloud-example-com",
                    "zone:zone-123:nextcloud.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "office-example-com",
                    "zone:zone-123:office.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "nextcloud-stack-shared",
                    "stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "nextcloud-stack-shared-postgres",
                    "stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_redis",
                    "nextcloud-stack-shared-redis",
                    "stack:nextcloud-stack:shared-redis",
                ),
                OwnedResource(
                    "shared_core_litellm",
                    "nextcloud-stack-shared-litellm",
                    "stack:nextcloud-stack:shared-litellm",
                ),
                OwnedResource(
                    "headscale_service",
                    "nextcloud-stack-headscale",
                    "stack:nextcloud-stack:headscale",
                ),
                OwnedResource(
                    "nextcloud_service",
                    "nextcloud-stack-nextcloud",
                    "stack:nextcloud-stack:nextcloud-service",
                ),
                OwnedResource(
                    "onlyoffice_service",
                    "nextcloud-stack-onlyoffice",
                    "stack:nextcloud-stack:onlyoffice-service",
                ),
                OwnedResource(
                    "nextcloud_volume",
                    "nextcloud-stack-nextcloud-data",
                    "stack:nextcloud-stack:nextcloud-volume",
                ),
                OwnedResource(
                    "onlyoffice_volume",
                    "nextcloud-stack-onlyoffice-data",
                    "stack:nextcloud-stack:onlyoffice-volume",
                ),
            ),
    )
    write_ownership_ledger(state_dir, ledger)
    seed_creation_authority(state_dir, ledger.resources)


def test_modify_disable_nextcloud_deletes_runtime_and_preserves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _seed_nextcloud_state(state_dir)
    modify_env = tmp_path / "disable-nextcloud.env"
    base_env = (FIXTURES_DIR / "nextcloud.env").read_text(encoding="utf-8")
    updated_env = _replace_line(base_env, "ENABLE_NEXTCLOUD", "false")
    updated_env += (
        "\nCLOUDFLARE_MOCK_EXISTING_TUNNEL_ID=nextcloud-stack-tunnel"
        "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com,nextcloud.example.com,office.example.com"
        "\nHEADSCALE_MOCK_EXISTING_SERVICE_ID=nextcloud-stack-headscale\n"
    )
    modify_env.write_text(updated_env, encoding="utf-8")

    deleted_resources: list[PlannedDeletion] = []

    class RecordingShellUninstallBackend:
        def __init__(
            self,
            raw_input: RawEnvInput,
            *,
            state_dir: Path | None = None,
            api_url: str | None = None,
        ) -> None:
            del raw_input, api_url
            if state_dir is None:
                raise AssertionError("recording backend requires a state directory")
            self.state_dir = state_dir

        def delete(self, deletion: PlannedDeletion) -> None:
            deleted_resources.append(deletion)
            UninstallAuthorityStore(self.state_dir).record_deletion(deletion.resource)

    monkeypatch.setattr(cli, "ShellUninstallBackend", RecordingShellUninstallBackend)

    summary = run_modify_flow(
        env_file=modify_env,
        state_dir=state_dir,
        dry_run=False,
    )

    loaded = load_state_dir(state_dir)
    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["disable_teardown"]["planned_deletions"]
    deleted_types = {
        item["resource_type"]
        for item in summary["disable_teardown"]["executed"]["deleted_resources"]
    }
    assert deleted_types == {
        "cloudflare_dns_record",
        "nextcloud_service",
        "onlyoffice_service",
    }
    assert deleted_resources
    assert loaded.ownership_ledger is not None
    authority = UninstallAuthorityStore(state_dir)
    assert all(authority.load_deletion(item.resource) is not None for item in deleted_resources)
    assert all(
        authority.load_created(resource) is not None
        and authority.load_deletion(resource) is None
        for resource in loaded.ownership_ledger.resources
    )
    assert {resource.resource_type for resource in loaded.ownership_ledger.resources} == {
        "cloudflare_tunnel",
        "cloudflare_dns_record",
        "headscale_service",
        "nextcloud_volume",
        "onlyoffice_volume",
        "shared_core_litellm",
        "shared_core_network",
        "shared_core_postgres",
    }
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
