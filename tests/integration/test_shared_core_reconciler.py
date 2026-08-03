# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.core import (
    SHARED_NETWORK_RESOURCE_TYPE,
    SHARED_POSTGRES_RESOURCE_TYPE,
    SharedCoreResourceRecord,
    SharedPostgresAllocation,
)
from dokploy_wizard.core.reconciler import SharedCoreError, reconcile_shared_core
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
    DokploySharedCoreBackend,
)
from dokploy_wizard.dokploy.shared_core_schedule import reconcile_owned_schedule
from dokploy_wizard.dokploy.shared_core_sync_runtime import SyncScheduleOutcome
from dokploy_wizard.litellm.config_renderer import verified_opencode_go_chat_model_ids
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.nextcloud import (
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudResourceRecord,
    TalkRuntime,
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
)
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    ScheduleSpec,
    SyncDesiredState,
    SyncOwnershipMetadata,
    SyncStateError,
    ensure_owner,
)
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_targets import DockerNetworkRecord
from tests.unit._opencode_go_reconciler_support import (
    MemoryModelAdminApi,
    reconciliation_input,
    sample_catalog_model,
)

from .task5_schedule_fakes import (
    ScheduleClient,
    applied_with_schedule,
)
from .task5_schedule_fakes import (
    desired as task5_desired,
)
from .task5_schedule_fakes import (
    record as task5_record,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def test_sync_plan_roundtrip() -> None:
    sync_desired = task5_desired()
    client = ScheduleClient()

    applied = reconcile_owned_schedule(
        client=client,
        desired=sync_desired,
        applied=AppliedSyncState.initial(sync_desired),
    )
    reused = reconcile_owned_schedule(
        client=client,
        desired=SyncDesiredState.from_dict(sync_desired.to_dict()),
        applied=AppliedSyncState.from_dict(applied.to_dict()),
    )

    assert reused == applied
    assert client.calls == ["list", "create", "list", "list", "list"]


def test_sync_resource_order() -> None:
    sync_desired = task5_desired()
    client = ScheduleClient()

    reconcile_owned_schedule(
        client=client,
        desired=sync_desired,
        applied=AppliedSyncState.initial(sync_desired),
    )

    assert client.calls == ["list", "create", "list"]
    assert client.schedules[0].command == sync_desired.schedule_spec.command


def test_malformed_schedule() -> None:
    sync_desired = task5_desired()
    malformed = task5_record(sync_desired)
    client = ScheduleClient(schedules=[replace(malformed, command="not-owner-marked")])

    with pytest.raises(SyncStateError, match="owner"):
        reconcile_owned_schedule(
            client=client,
            desired=sync_desired,
            applied=AppliedSyncState.initial(sync_desired),
        )

    assert client.calls == ["list"]


def test_unknown_sync_resource() -> None:
    sync_desired = task5_desired()
    client = ScheduleClient()

    with pytest.raises(SyncStateError, match="missing"):
        reconcile_owned_schedule(
            client=client,
            desired=sync_desired,
            applied=applied_with_schedule(sync_desired, "missing"),
        )

    assert client.calls == ["list"]


def _write_empty_applied_checkpoint(state_dir: Path) -> None:
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("shared_core",),
        ),
    )


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool
    healthy_after_install: bool
    install_calls: int = 0

    def is_healthy(self) -> bool:
        if self.install_calls == 0:
            return self.healthy_before_install
        return self.healthy_after_install

    def install(self) -> None:
        self.install_calls += 1


@dataclass
class FakeCloudflareBackend:
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, CloudflareDnsRecord] = field(default_factory=dict)
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)

    def validate_account_access(self, account_id: str) -> None:
        return None

    def validate_zone_access(self, zone_id: str) -> None:
        return None

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        self.existing_tunnel = CloudflareTunnel(tunnel_id="nextcloud-tunnel", name=tunnel_name)
        return self.existing_tunnel

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        del account_id, tunnel_id, ingress

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        record = self.dns_records.get(hostname)
        if record is None:
            return ()
        if content is not None and record.content != content:
            return ()
        return (record,)

    def get_dns_record(self, zone_id: str, record_id: str) -> CloudflareDnsRecord | None:
        del zone_id
        return next(
            (record for record in self.dns_records.values() if record.record_id == record_id),
            None,
        )

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        record = CloudflareDnsRecord(
            record_id=f"dns-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )
        self.dns_records[hostname] = record
        return record

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.provider_id == provider_id:
            return self.access_provider
        return None

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.name == name:
            return self.access_provider
        return None

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        del account_id
        self.access_provider = CloudflareAccessIdentityProvider(
            provider_id="otp-provider-1",
            name=name,
            provider_type="onetimepin",
        )
        return self.access_provider

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return next((item for item in self.access_apps.values() if item.app_id == app_id), None)

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return self.access_apps.get(domain)

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
        del account_id
        app = CloudflareAccessApplication(
            app_id=f"app-{domain}",
            name=name,
            domain=domain,
            app_type="self_hosted",
            allowed_identity_provider_ids=allowed_identity_provider_ids,
        )
        self.access_apps[domain] = app
        return app

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        del account_id, policy_id
        return self.access_policies.get(app_id)

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
        del account_id
        policy = self.access_policies.get(app_id)
        if policy is not None and policy.name == name:
            return policy
        return None

    def create_access_policy(
        self,
        account_id: str,
        *,
        app_id: str,
        name: str,
        emails: tuple[str, ...],
    ) -> CloudflareAccessPolicy:
        del account_id
        policy = CloudflareAccessPolicy(
            policy_id=f"policy-{app_id}",
            app_id=app_id,
            name=name,
            decision="allow",
            emails=emails,
        )
        self.access_policies[app_id] = policy
        return policy


@dataclass
class FakeSharedCoreBackend:
    network: SharedCoreResourceRecord | None = None
    postgres: SharedCoreResourceRecord | None = None
    redis: SharedCoreResourceRecord | None = None
    mail_relay: SharedCoreResourceRecord | None = None
    litellm: SharedCoreResourceRecord | None = None
    create_network_calls: int = 0
    create_postgres_calls: int = 0
    create_redis_calls: int = 0
    create_mail_relay_calls: int = 0
    create_litellm_calls: int = 0
    ensured_allocations: tuple[SharedPostgresAllocation, ...] = ()
    refresh_compose_calls: int = 0
    reconcile_litellm_runtime_calls: int = 0
    call_order: list[str] = field(default_factory=list)

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_network_calls += 1
        self.call_order.append("create_network")
        self.network = SharedCoreResourceRecord(
            resource_id="network-1",
            resource_name=resource_name,
        )
        return self.network

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.postgres is not None and self.postgres.resource_id == resource_id:
            return self.postgres
        return None

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.postgres is not None and self.postgres.resource_name == resource_name:
            return self.postgres
        return None

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_postgres_calls += 1
        self.call_order.append("create_postgres_service")
        self.postgres = SharedCoreResourceRecord(
            resource_id="postgres-1",
            resource_name=resource_name,
        )
        return self.postgres

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.redis is not None and self.redis.resource_id == resource_id:
            return self.redis
        return None

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.redis is not None and self.redis.resource_name == resource_name:
            return self.redis
        return None

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_redis_calls += 1
        self.call_order.append("create_redis_service")
        self.redis = SharedCoreResourceRecord(
            resource_id="redis-1",
            resource_name=resource_name,
        )
        return self.redis

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.mail_relay is not None and self.mail_relay.resource_id == resource_id:
            return self.mail_relay
        return None

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.mail_relay is not None and self.mail_relay.resource_name == resource_name:
            return self.mail_relay
        return None

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_mail_relay_calls += 1
        self.call_order.append("create_mail_relay_service")
        self.mail_relay = SharedCoreResourceRecord(
            resource_id="postfix-1",
            resource_name=resource_name,
        )
        return self.mail_relay

    def get_litellm_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.litellm is not None and self.litellm.resource_id == resource_id:
            return self.litellm
        return None

    def find_litellm_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.litellm is not None and self.litellm.resource_name == resource_name:
            return self.litellm
        return None

    def create_litellm_service(self, resource_name: str) -> SharedCoreResourceRecord:
        self.create_litellm_calls += 1
        self.call_order.append("create_litellm_service")
        self.litellm = SharedCoreResourceRecord(
            resource_id="litellm-1",
            resource_name=resource_name,
        )
        return self.litellm

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        self.call_order.append("ensure_postgres_allocations")
        self.ensured_allocations = allocations

    def refresh_compose(self) -> None:
        self.refresh_compose_calls += 1
        self.call_order.append("refresh_compose")

    def reconcile_litellm_runtime(self) -> None:
        self.reconcile_litellm_runtime_calls += 1
        self.call_order.append("reconcile_litellm_runtime")


@dataclass
class AuthorityRecordingFakeSharedCoreBackend(FakeSharedCoreBackend):
    authority_record_calls: int = 0
    authority_resources: tuple[OwnedResource, ...] = ()

    def record_created_uninstall_authorities(
        self,
        authority_store: UninstallAuthorityStore,
        resources: tuple[OwnedResource, ...],
    ) -> None:
        del authority_store
        self.authority_record_calls += 1
        self.authority_resources = resources
        self.call_order.append("record_created_uninstall_authorities")


class SyncingFakeSharedCoreBackend(FakeSharedCoreBackend):
    def __init__(self, state_dir: Path, stack_name: str) -> None:
        super().__init__()
        self.state_dir = state_dir
        self.stack_name = stack_name
        self.sync_calls = 0

    def reconcile_sync_schedule(
        self,
        *,
        existing_applied: AppliedSyncState | None,
        existing_metadata: SyncOwnershipMetadata | None,
    ) -> SyncScheduleOutcome:
        del existing_metadata
        self.sync_calls += 1
        owner = ensure_owner(self.state_dir / "shared-core-sync-owner.json")
        schedule = ScheduleSpec.for_shared_core(
            stack_name=self.stack_name,
            compose_id="compose-1",
            owner_id=owner.owner_id,
        )
        desired = SyncDesiredState.from_schedule(
            owner_id=owner.owner_id,
            config_sha256="a" * 64,
            litellm_image_digest="ghcr.io/berriai/litellm@sha256:" + "b" * 64,
            metadata_volume=f"{self.stack_name}-shared-litellm-data",
            schedule_spec=schedule,
        )
        return SyncScheduleOutcome(
            desired=desired,
            applied=existing_applied or AppliedSyncState.initial(desired),
            metadata=None,
        )


@dataclass
class FakeDokployApiClient(ScheduleClient):
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        environment = DokployEnvironmentSummary(
            environment_id="env-1",
            name="production",
            is_default=True,
            composes=(),
        )
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(environment,),
            )
        )
        return DokployCreatedProject(project_id="proj-1", environment_id="env-1")

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del compose_file, app_name
        self.create_compose_calls += 1
        record = DokployComposeRecord(compose_id="cmp-1", name=name)
        self.projects[0] = DokployProjectSummary(
            project_id=self.projects[0].project_id,
            name=self.projects[0].name,
            environments=(
                DokployEnvironmentSummary(
                    environment_id=environment_id,
                    name="production",
                    is_default=True,
                    composes=(
                        DokployComposeSummary(
                            compose_id=record.compose_id,
                            name=record.name,
                            status=None,
                        ),
                    ),
                ),
            ),
        )
        return record

    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord:
        del env
        if compose_file is not None:
            self.update_compose_calls += 1
        return DokployComposeRecord(compose_id=compose_id, name="nextcloud-stack-shared")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


@dataclass
class AuthorityDockerClient:
    network: DockerNetworkRecord

    def get_network(self, network_id: str) -> DockerNetworkRecord | None:
        assert network_id == self.network.network_id
        return self.network


def test_db_cutover_applies_static_then_dynamic_compose_and_persists_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    desired = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "LITELLM_OPENCODE_GO_API_KEY": "test-upstream-key",
            },
        )
    )
    models = tuple(
        sample_catalog_model(source_id)
        for source_id in sorted(verified_opencode_go_chat_model_ids())
    )
    client = FakeDokployApiClient()
    model_api = MemoryModelAdminApi([], [], [], [])
    _write_empty_applied_checkpoint(tmp_path)
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.test/api",
        api_key="test-key",
        stack_name=desired.stack_name,
        plan=desired.shared_core,
        litellm_env={"LITELLM_OPENCODE_GO_API_KEY": "test-upstream-key"},
        runtime_images=desired.runtime_images,
        client=client,
        state_dir=tmp_path,
        litellm_model_admin_api=model_api,
        opencode_go_cutover_input=reconciliation_input(models),
        opencode_go_cutover_state_root=tmp_path / "cutover-state",
    )
    monkeypatch.setattr(backend, "_verify_cutover_runtime", lambda: None)
    backend.create_network(desired.shared_core.network_name)

    # When
    backend._run_opencode_go_cutover()

    # Then
    assert client.deploy_calls == 3
    assert client.update_compose_calls == 3
    assert len(model_api.mutations) == len(models)
    assert (
        tmp_path
        / "cutover-state"
        / "opencode-go"
        / "litellm-cutover-receipt-v1.json"
    ).exists()


def test_schedule_owner_id_production_reconciliation_persists_receipt(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    desired = resolve_desired_state(raw)
    client = FakeDokployApiClient()
    _write_empty_applied_checkpoint(tmp_path)
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.test/api",
        api_key="test-key",
        stack_name=desired.stack_name,
        plan=desired.shared_core,
        litellm_env={"LITELLM_OPENCODE_GO_API_KEY": "test-upstream-key"},
        runtime_images=desired.runtime_images,
        client=client,
        state_dir=tmp_path,
    )
    backend.create_network(desired.shared_core.network_name)

    outcome = backend.reconcile_sync_schedule(
        existing_applied=None,
        existing_metadata=None,
    )

    assert outcome is not None
    assert outcome.desired.schedule_spec.command.startswith(
        f"DOKPLOY_WIZARD_SCHEDULE_OWNER_ID={outcome.desired.owner_id} TZ=UTC "
    )
    receipt_sha256 = outcome.applied.last_sync_receipt_sha256
    assert receipt_sha256 is not None
    receipt = SyncArtifactStore(tmp_path).load_schedule_receipt(receipt_sha256)
    assert receipt.remote == client.schedules[0]
    assert client.calls == ["list", "create", "list"]


def test_new_shared_core_compose_records_re_read_authority_for_compose_and_network(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    desired = resolve_desired_state(raw)
    client = FakeDokployApiClient()
    _write_empty_applied_checkpoint(tmp_path)
    network = DockerNetworkRecord(
        network_id=desired.shared_core.network_name,
        name=desired.shared_core.network_name,
    )
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.test/api",
        api_key="test-key",
        stack_name=desired.stack_name,
        plan=desired.shared_core,
        client=client,
        state_dir=tmp_path,
        authority_docker_client=AuthorityDockerClient(network),
    )
    backend.create_network(desired.shared_core.network_name)
    resources = (
        OwnedResource(
            resource_type=SHARED_NETWORK_RESOURCE_TYPE,
            resource_id="dokploy-compose:cmp-1:network",
            scope=f"stack:{desired.stack_name}:shared-network",
        ),
        OwnedResource(
            resource_type=SHARED_POSTGRES_RESOURCE_TYPE,
            resource_id="dokploy-compose:cmp-1:postgres",
            scope=f"stack:{desired.stack_name}:shared-postgres",
        ),
    )
    authorities = UninstallAuthorityStore(tmp_path)

    backend.record_created_uninstall_authorities(authorities, resources)

    compose_authority = authorities.load_created(resources[1])
    network_authority = authorities.load_created(resources[0])
    assert compose_authority is not None
    assert compose_authority.provider == "dokploy_compose"
    assert compose_authority.physical_target_id == "cmp-1"
    assert compose_authority.parent_target_id == "proj-1"
    assert network_authority is not None
    assert network_authority.provider == "docker_network"
    assert network_authority.physical_target_id == desired.shared_core.network_name



@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        del hostname, secret_refs
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return True


@dataclass
class FakeNextcloudBackend:
    services: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    volumes: dict[str, NextcloudResourceRecord] = field(default_factory=dict)

    def get_service(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self.services.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_service_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        return self.services.get(resource_name)

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del hostname, data_volume_name, config
        record = NextcloudResourceRecord(
            resource_id=f"service:{resource_name}",
            resource_name=resource_name,
        )
        self.services[resource_name] = record
        return record

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            data_volume_name=data_volume_name,
            config=config,
        )

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self.volumes.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        return self.volumes.get(resource_name)

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord:
        record = NextcloudResourceRecord(
            resource_id=f"volume:{resource_name}",
            resource_name=resource_name,
        )
        self.volumes[resource_name] = record
        return record

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        del service, url
        return True

    def ensure_application_ready(
        self, *, nextcloud_url: str, onlyoffice_url: str
    ) -> NextcloudBundleVerification:
        del nextcloud_url, onlyoffice_url
        return NextcloudBundleVerification(
            onlyoffice_document_server_check=NextcloudCommandCheck(
                command="php occ onlyoffice:documentserver --check",
                passed=True,
            ),
            talk=TalkRuntime(
                app_id="spreed",
                enabled=True,
                enabled_check=NextcloudCommandCheck(
                    command="php occ app:list --output=json",
                    passed=True,
                ),
                signaling_check=NextcloudCommandCheck(
                    command="php occ talk:signaling:list --output=json",
                    passed=True,
                ),
                stun_check=NextcloudCommandCheck(
                    command="php occ talk:stun:list --output=json",
                    passed=True,
                ),
                turn_check=NextcloudCommandCheck(
                    command="php occ talk:turn:list --output=json",
                    passed=True,
                ),
            ),
        )

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        del admin_user


def test_install_plans_and_persists_shared_core_once_for_nextcloud(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=FakeNextcloudBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["shared_core"]["outcome"] == "applied"
    assert summary["shared_core"]["network"]["resource_name"] == "nextcloud-stack-shared"
    assert summary["shared_core"]["postgres"]["resource_name"] == "nextcloud-stack-shared-postgres"
    assert summary["shared_core"]["redis"]["resource_name"] == "nextcloud-stack-shared-redis"
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
        "nextcloud",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:headscale.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:nextcloud.example.com"),
        ("cloudflare_dns_record", "zone:zone-123:office.example.com"),
        ("headscale_service", "stack:nextcloud-stack:headscale"),
        ("shared_core_litellm", "stack:nextcloud-stack:shared-litellm"),
        ("shared_core_network", "stack:nextcloud-stack:shared-network"),
        ("shared_core_postgres", "stack:nextcloud-stack:shared-postgres"),
        ("shared_core_redis", "stack:nextcloud-stack:shared-redis"),
        ("nextcloud_service", "stack:nextcloud-stack:nextcloud-service"),
        ("onlyoffice_service", "stack:nextcloud-stack:onlyoffice-service"),
        ("nextcloud_volume", "stack:nextcloud-stack:nextcloud-volume"),
        ("onlyoffice_volume", "stack:nextcloud-stack:onlyoffice-volume"),
    }


def test_install_records_shared_core_authority_after_creating_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    shared_core = AuthorityRecordingFakeSharedCoreBackend()

    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=shared_core,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=FakeNextcloudBackend(),
    )

    assert shared_core.authority_record_calls == 1
    assert {
        (resource.resource_type, resource.scope)
        for resource in shared_core.authority_resources
        if resource.resource_type.startswith("shared_core_")
    } == {
        ("shared_core_litellm", "stack:nextcloud-stack:shared-litellm"),
        ("shared_core_network", "stack:nextcloud-stack:shared-network"),
        ("shared_core_postgres", "stack:nextcloud-stack:shared-postgres"),
        ("shared_core_redis", "stack:nextcloud-stack:shared-redis"),
    }
    assert shared_core.call_order.index("create_network") < shared_core.call_order.index(
        "record_created_uninstall_authorities"
    )
    assert shared_core.call_order.index("create_postgres_service") < shared_core.call_order.index(
        "record_created_uninstall_authorities"
    )
    assert shared_core.call_order.index("create_redis_service") < shared_core.call_order.index(
        "record_created_uninstall_authorities"
    )
    assert shared_core.call_order.index("create_litellm_service") < shared_core.call_order.index(
        "record_created_uninstall_authorities"
    )


def test_immediate_sync_due_boundaries_run_owner_first_projection_through_install(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    raw = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    desired = resolve_desired_state(raw)
    shared = SyncingFakeSharedCoreBackend(state_dir, desired.stack_name)

    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=shared,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=FakeNextcloudBackend(),
    )

    loaded = load_state_dir(state_dir)
    intent = (state_dir / "state-upgrade-intent-v1.json").read_text(encoding="utf-8")
    assert shared.sync_calls == 1
    assert loaded.desired_state is not None
    assert loaded.desired_state.opencode_go_sync is not None
    assert loaded.applied_state is not None
    assert loaded.applied_state.opencode_go_sync is not None
    assert '"status":"complete"' in intent


def test_completed_outer_v1_legacy_state_reconciles_images_and_sync_via_lifecycle(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    raw = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    networking = FakeCloudflareBackend()
    headscale = FakeHeadscaleBackend()
    nextcloud = FakeNextcloudBackend()
    initial_shared = FakeSharedCoreBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking,
        shared_core_backend=initial_shared,
        headscale_backend=headscale,
        nextcloud_backend=nextcloud,
    )
    installed = load_state_dir(state_dir)
    assert installed.desired_state is not None
    assert installed.applied_state is not None
    legacy_desired = installed.desired_state.to_dict()
    legacy_desired.pop("runtime_images")
    legacy_desired["shared_core"].pop("opencode_go_sync", None)
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    legacy_applied = installed.applied_state.to_dict()
    legacy_applied.pop("runtime_images", None)
    legacy_applied.pop("opencode_go_sync", None)
    legacy_applied["desired_state_fingerprint"] = legacy_fingerprint
    (state_dir / "desired-state.json").write_text(
        json.dumps(legacy_desired),
        encoding="utf-8",
    )
    (state_dir / "applied-state.json").write_text(
        json.dumps(legacy_applied),
        encoding="utf-8",
    )
    shared = SyncingFakeSharedCoreBackend(
        state_dir,
        installed.desired_state.stack_name,
    )
    shared.network = initial_shared.network
    shared.postgres = initial_shared.postgres
    shared.redis = initial_shared.redis
    shared.mail_relay = initial_shared.mail_relay
    shared.litellm = initial_shared.litellm

    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking,
        shared_core_backend=shared,
        headscale_backend=headscale,
        nextcloud_backend=nextcloud,
    )

    migrated = load_state_dir(state_dir)
    assert shared.sync_calls == 1
    assert migrated.applied_state is not None
    assert migrated.applied_state.runtime_images is not None
    assert migrated.applied_state.opencode_go_sync is not None
    assert (
        migrated.applied_state.opencode_go_sync.previous_legacy_fingerprint
        == legacy_fingerprint
    )


def test_install_rerun_reuses_owned_shared_core_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    shared_backend = FakeSharedCoreBackend()
    headscale_backend = FakeHeadscaleBackend()
    nextcloud_backend = FakeNextcloudBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        nextcloud_backend=nextcloud_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="nextcloud-tunnel",
                name="nextcloud-stack-tunnel",
            ),
            dns_records={
                "dokploy.example.com": CloudflareDnsRecord(
                    record_id="dns-dokploy.example.com",
                    name="dokploy.example.com",
                    record_type="CNAME",
                    content="nextcloud-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "headscale.example.com": CloudflareDnsRecord(
                    record_id="dns-headscale.example.com",
                    name="headscale.example.com",
                    record_type="CNAME",
                    content="nextcloud-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "nextcloud.example.com": CloudflareDnsRecord(
                    record_id="dns-nextcloud.example.com",
                    name="nextcloud.example.com",
                    record_type="CNAME",
                    content="nextcloud-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
                "office.example.com": CloudflareDnsRecord(
                    record_id="dns-office.example.com",
                    name="office.example.com",
                    record_type="CNAME",
                    content="nextcloud-tunnel.cfargotunnel.com",
                    proxied=True,
                ),
            },
        ),
        shared_core_backend=shared_backend,
        headscale_backend=headscale_backend,
        nextcloud_backend=nextcloud_backend,
    )

    assert summary["shared_core"]["outcome"] == "already_present"
    assert summary["shared_core"]["litellm"]["action"] == "reuse_owned"
    assert summary["shared_core"]["network"]["action"] == "reuse_owned"
    assert summary["shared_core"]["postgres"]["action"] == "reuse_owned"
    assert summary["shared_core"]["redis"]["action"] == "reuse_owned"
    assert shared_backend.create_litellm_calls == 1
    assert shared_backend.create_network_calls == 1
    assert shared_backend.create_postgres_calls == 1
    assert shared_backend.create_redis_calls == 1
    assert shared_backend.ensured_allocations == (
        SharedPostgresAllocation(
            database_name="nextcloud_stack_nextcloud",
            user_name="nextcloud_stack_nextcloud",
            password_secret_ref="nextcloud-stack-nextcloud-postgres-password",
        ),
        SharedPostgresAllocation(
            database_name="nextcloud_stack_litellm",
            user_name="nextcloud_stack_litellm",
            password_secret_ref="nextcloud-stack-litellm-postgres-password",
        ),
    )


def test_install_fails_closed_when_shared_core_owned_resource_drifted(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    headscale_backend = FakeHeadscaleBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=headscale_backend,
        nextcloud_backend=FakeNextcloudBackend(),
    )

    with pytest.raises(SharedCoreError, match="shared-core resource 'shared_core_network' exists"):
        run_install_flow(
            env_file=FIXTURES_DIR / "nextcloud.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(
                existing_tunnel=CloudflareTunnel(
                    tunnel_id="nextcloud-tunnel",
                    name="nextcloud-stack-tunnel",
                ),
                dns_records={
                    "dokploy.example.com": CloudflareDnsRecord(
                        record_id="dns-dokploy.example.com",
                        name="dokploy.example.com",
                        record_type="CNAME",
                        content="nextcloud-tunnel.cfargotunnel.com",
                        proxied=True,
                    ),
                    "headscale.example.com": CloudflareDnsRecord(
                        record_id="dns-headscale.example.com",
                        name="headscale.example.com",
                        record_type="CNAME",
                        content="nextcloud-tunnel.cfargotunnel.com",
                        proxied=True,
                    ),
                    "nextcloud.example.com": CloudflareDnsRecord(
                        record_id="dns-nextcloud.example.com",
                        name="nextcloud.example.com",
                        record_type="CNAME",
                        content="nextcloud-tunnel.cfargotunnel.com",
                        proxied=True,
                    ),
                    "office.example.com": CloudflareDnsRecord(
                        record_id="dns-office.example.com",
                        name="office.example.com",
                        record_type="CNAME",
                        content="nextcloud-tunnel.cfargotunnel.com",
                        proxied=True,
                    ),
                },
            ),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=headscale_backend,
            nextcloud_backend=FakeNextcloudBackend(),
        )


def test_dokploy_shared_core_backend_creates_project_compose_and_reuses_owned_resources(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    _write_empty_applied_checkpoint(state_dir)
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_API_KEY": "dokp-key-123",
            },
        )
    )
    client = FakeDokployApiClient()
    provisioned: list[tuple[SharedPostgresAllocation, ...]] = []
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        client=client,
        allocation_provisioner=lambda allocations: provisioned.append(allocations),
        state_dir=state_dir,
    )
    setattr(backend, "_wait_for_shared_core_containers", lambda: None)

    phase = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.network is not None
    assert phase.result.postgres is not None
    assert phase.result.redis is not None
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert provisioned == [
        (
            SharedPostgresAllocation(
                database_name="nextcloud_stack_nextcloud",
                user_name="nextcloud_stack_nextcloud",
                password_secret_ref="nextcloud-stack-nextcloud-postgres-password",
            ),
            SharedPostgresAllocation(
                database_name="nextcloud_stack_litellm",
                user_name="nextcloud_stack_litellm",
                password_secret_ref="nextcloud-stack-litellm-postgres-password",
            ),
        )
    ]

    reused = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="shared_core_network",
                    resource_id=phase.network_resource_id or "",
                    scope="stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    resource_type="shared_core_postgres",
                    resource_id=phase.postgres_resource_id or "",
                    scope="stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    resource_type="shared_core_redis",
                    resource_id=phase.redis_resource_id or "",
                    scope="stack:nextcloud-stack:shared-redis",
                ),
            ),
        ),
        backend=backend,
    )

    assert reused.result.outcome == "already_present"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.update_compose_calls == 1
    assert client.deploy_calls == 1


def test_shared_core_reconciles_litellm_after_postgres_allocations() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = FakeSharedCoreBackend()

    phase = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert backend.refresh_compose_calls == 1
    assert backend.reconcile_litellm_runtime_calls == 1
    assert backend.call_order.index("ensure_postgres_allocations") < backend.call_order.index(
        "reconcile_litellm_runtime"
    )


def test_dokploy_shared_core_backend_defers_litellm_runtime_until_explicit_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    _write_empty_applied_checkpoint(state_dir)
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_API_KEY": "dokp-key-123",
            },
        )
    )
    client = FakeDokployApiClient()
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        client=client,
        state_dir=state_dir,
    )
    setattr(backend, "_wait_for_shared_core_containers", lambda: None)
    reconcile_calls: list[str] = []
    monkeypatch.setattr(
        backend,
        "_ensure_litellm_runtime_ready_and_reconciled",
        lambda: reconcile_calls.append("reconciled"),
    )

    backend.create_network(desired_state.shared_core.network_name)

    assert reconcile_calls == []

    backend.reconcile_litellm_runtime()

    assert reconcile_calls == ["reconciled"]


def test_dokploy_shared_core_backend_updates_existing_compose_when_mail_relay_container_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    _write_empty_applied_checkpoint(state_dir)
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "docuseal-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_DOCUSEAL": "true",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_API_KEY": "dokp-key-123",
            },
        )
    )
    existing_project = DokployProjectSummary(
        project_id="proj-1",
        name=desired_state.stack_name,
        environments=(
            DokployEnvironmentSummary(
                environment_id="env-1",
                name="production",
                is_default=True,
                composes=(
                    DokployComposeSummary(
                        compose_id="cmp-1",
                        name=desired_state.shared_core.network_name,
                        status="running",
                    ),
                ),
            ),
        ),
    )
    client = FakeDokployApiClient(projects=[existing_project])
    provisioned: list[tuple[SharedPostgresAllocation, ...]] = []
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        client=client,
        allocation_provisioner=lambda allocations: provisioned.append(allocations),
        state_dir=state_dir,
    )
    setattr(backend, "_wait_for_shared_core_containers", lambda: None)
    assert desired_state.shared_core.mail_relay is not None
    mail_relay_service_name = desired_state.shared_core.mail_relay.service_name
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.shared_core._find_container_name",
        lambda service_name: None
        if service_name == mail_relay_service_name
        else "present-container",
    )

    phase = reconcile_shared_core(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.network is not None
    assert phase.result.network.action == "reuse_existing"
    assert phase.result.postgres is not None
    assert phase.result.postgres.action == "reuse_existing"
    assert phase.result.mail_relay is not None
    assert phase.result.mail_relay.action == "create"
    assert phase.mail_relay_resource_id == "dokploy-compose:cmp-1:postfix"
    assert client.create_project_calls == 0
    assert client.create_compose_calls == 0
    assert client.update_compose_calls == 1
    assert client.deploy_calls == 1
    assert provisioned == [
        (
            SharedPostgresAllocation(
                database_name="docuseal_stack_docuseal",
                user_name="docuseal_stack_docuseal",
                password_secret_ref="docuseal-stack-docuseal-postgres-password",
            ),
            SharedPostgresAllocation(
                database_name="docuseal_stack_litellm",
                user_name="docuseal_stack_litellm",
                password_secret_ref="docuseal-stack-litellm-postgres-password",
            ),
        )
    ]
