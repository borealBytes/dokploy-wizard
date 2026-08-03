from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.client import (
    DokployComposeSummary,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_authority_schema import UninstallAuthorityError
from dokploy_wizard.state.uninstall_targets import DockerVolumeRecord
from dokploy_wizard.uninstall.executor import execute_uninstall_plan
from dokploy_wizard.uninstall.families import ProviderFamily, family_for
from dokploy_wizard.uninstall.lifecycle_authority import LifecycleAuthorityPublisher
from dokploy_wizard.uninstall.planner import _RULES, PlannedDeletion, UninstallPlan

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass(frozen=True, slots=True)
class DokployReader:
    project: DokployProjectSummary

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return (self.project,)


@dataclass(frozen=True, slots=True)
class DockerReader:
    volumes: tuple[DockerVolumeRecord, ...]

    def get_volume(self, volume_id: str) -> DockerVolumeRecord | None:
        return next((volume for volume in self.volumes if volume.volume_id == volume_id), None)


@dataclass(frozen=True, slots=True)
class NoopBackend:
    def delete(self, deletion: PlannedDeletion) -> None:
        del deletion


def _publisher(store: UninstallAuthorityStore) -> LifecycleAuthorityPublisher:
    compose = DokployComposeSummary(compose_id="compose-1", name="wizard-nextcloud", status=None)
    project = DokployProjectSummary(
        project_id="project-1",
        name="wizard",
        environments=(
            DokployEnvironmentSummary(
                environment_id="environment-1",
                name="production",
                is_default=True,
                composes=(compose,),
            ),
        ),
    )
    docker = DockerReader(
        tuple(
            DockerVolumeRecord(volume_id=f"volume-{resource_type}", name=f"volume-{resource_type}")
            for resource_type in _RULES
            if family_for(resource_type) is ProviderFamily.DOCKER
        )
    )
    return LifecycleAuthorityPublisher(store, DokployReader(project), docker, "wizard")


def test_created_generic_resources_publish_authority_for_every_dokploy_and_docker_rule(
    tmp_path: Path,
) -> None:
    store = UninstallAuthorityStore(tmp_path)
    publisher = _publisher(store)

    for resource_type in _RULES:
        family = family_for(resource_type)
        if family in {
            ProviderFamily.SCHEDULE,
            ProviderFamily.CLOUDFLARE,
            ProviderFamily.TAILSCALE,
        }:
            continue
        resource = OwnedResource(resource_type, f"logical-{resource_type}", "stack:wizard:test")
        match family:
            case ProviderFamily.DOKPLOY:
                publisher.record_created_compose(
                    resource,
                    "dokploy-compose:compose-1:service",
                )
            case ProviderFamily.DOCKER:
                publisher.record_created_volume(resource, f"volume-{resource_type}")
            case unreachable:
                raise AssertionError(f"Unexpected authority family: {unreachable}")

        assert store.load_created(resource) is not None


def test_created_generic_resource_rejects_missing_post_create_reread(tmp_path: Path) -> None:
    store = UninstallAuthorityStore(tmp_path)
    publisher = _publisher(store)
    resource = OwnedResource("nextcloud_service", "logical-nextcloud", "stack:wizard:test")

    with pytest.raises(UninstallAuthorityError, match="absent"):
        publisher.record_created_compose(resource, "dokploy-compose:missing:service")

    assert store.load_created(resource) is None


def test_noop_backend_cannot_remove_a_receipt_bound_ledger_resource(tmp_path: Path) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    resource = OwnedResource("nextcloud_service", "logical-nextcloud", "stack:wizard:nextcloud")
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    authority = UninstallAuthorityStore(tmp_path)
    authority.record_created(
        resource=resource,
        owner_id=_OWNER,
        provider="dokploy_compose",
        physical_target_id="compose-1",
        parent_target_id="project-1",
        expected_fingerprint="a" * 64,
    )
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=("preflight",),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    plan = UninstallPlan(
        mode="retain",
        environment=desired.stack_name,
        deletions=(PlannedDeletion(resource, "nextcloud", "retain_safe"),),
        retained_resources=(),
        warnings=(),
    )

    with pytest.raises(RuntimeError, match="deletion receipt"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=NoopBackend(),
            dry_run=False,
        )

    assert (tmp_path / "ownership-ledger.json").is_file()
