# pyright: reportMissingImports=false

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib import request

import pytest

import dokploy_wizard.dokploy.nextcloud as nextcloud_module
from dokploy_wizard.cli import _build_nextcloud_backend
from dokploy_wizard.core.models import SharedCorePlan
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployNextcloudBackend,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.client import DokployApiError
from dokploy_wizard.dokploy.nextcloud import (
    _ensure_trusted_domain,
    _ensure_onlyoffice_app_config,
    _local_https_health_check,
    _nextcloud_status_ready,
    _www_data_occ_shell,
    _with_trailing_slash,
)
from dokploy_wizard.packs.nextcloud import (
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
    NextcloudError,
    NextcloudResourceRecord,
    build_nextcloud_ledger,
    reconcile_nextcloud,
)
from dokploy_wizard.packs.nextcloud.models import NextcloudOpenClawWorkspaceContract
from dokploy_wizard.packs.nextcloud.models import NextcloudBundleVerification, NextcloudCommandCheck, TalkRuntime
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeNextcloudBackend:
    services: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    volumes: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    health: dict[str, bool] = field(default_factory=dict)
    create_service_calls: int = 0
    update_service_calls: int = 0
    create_volume_calls: int = 0

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
        self.create_service_calls += 1
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
        del hostname, data_volume_name, config
        self.update_service_calls += 1
        record = NextcloudResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
        )
        self.services[resource_name] = record
        return record

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self.volumes.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        return self.volumes.get(resource_name)

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord:
        self.create_volume_calls += 1
        record = NextcloudResourceRecord(
            resource_id=f"volume:{resource_name}",
            resource_name=resource_name,
        )
        self.volumes[resource_name] = record
        return record

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        del url
        return self.health.get(service.resource_name, True)

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


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    schedules: list[DokployScheduleRecord] = field(default_factory=list)
    schedule_auth_error: str | None = None
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0
    last_create_compose_file: str | None = None
    last_update_compose_file: str | None = None

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(
                    DokployEnvironmentSummary(
                        environment_id="env-1",
                        name="production",
                        is_default=True,
                        composes=(),
                    ),
                ),
            )
        )
        return DokployCreatedProject(project_id="proj-1", environment_id="env-1")

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del app_name
        self.create_compose_calls += 1
        self.last_create_compose_file = compose_file
        record = DokployComposeRecord(compose_id="cmp-1", name=name)
        self.projects[0] = DokployProjectSummary(
            project_id="proj-1",
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

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.update_compose_calls += 1
        self.last_update_compose_file = compose_file
        return DokployComposeRecord(compose_id=compose_id, name="wizard-stack-nextcloud")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        del compose_id
        if self.schedule_auth_error is not None:
            raise DokployApiError(self.schedule_auth_error)
        return tuple(self.schedules)

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        del compose_id
        if self.schedule_auth_error is not None:
            raise DokployApiError(self.schedule_auth_error)
        record = DokployScheduleRecord(
            schedule_id="sch-1",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record

    def update_schedule(
        self,
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
        del compose_id
        if self.schedule_auth_error is not None:
            raise DokployApiError(self.schedule_auth_error)
        record = DokployScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record


def test_reconcile_nextcloud_plans_paired_runtime_when_enabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )

    phase = reconcile_nextcloud(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeNextcloudBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.enabled is True
    assert phase.result.nextcloud is not None
    assert phase.result.nextcloud.hostname == "nextcloud.example.com"
    assert phase.result.nextcloud.config.onlyoffice_url == "https://office.example.com"
    assert phase.result.nextcloud.config.postgres.database_name == "wizard_stack_nextcloud"
    assert phase.result.nextcloud.config.redis.identity_name == "wizard-stack-nextcloud-redis"
    assert phase.result.nextcloud.health_check.passed is None
    assert phase.result.onlyoffice is not None
    assert phase.result.onlyoffice.hostname == "office.example.com"
    assert phase.result.onlyoffice.config.nextcloud_url == "https://nextcloud.example.com"
    assert (
        phase.result.onlyoffice.config.integration_secret_ref
        == "wizard-stack-nextcloud-onlyoffice-jwt-secret"
    )
    assert phase.result.onlyoffice.health_check.passed is None
    assert phase.result.talk is None


def test_reconcile_nextcloud_skips_cleanly_when_disabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "false",
            },
        )
    )

    phase = reconcile_nextcloud(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeNextcloudBackend(),
    )

    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False


def test_reconcile_nextcloud_reuses_owned_resources_and_requires_both_health_checks() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = FakeNextcloudBackend(
        services={
            "wizard-stack-nextcloud": NextcloudResourceRecord(
                resource_id="service:wizard-stack-nextcloud",
                resource_name="wizard-stack-nextcloud",
            ),
            "wizard-stack-onlyoffice": NextcloudResourceRecord(
                resource_id="service:wizard-stack-onlyoffice",
                resource_name="wizard-stack-onlyoffice",
            ),
        },
        volumes={
            "wizard-stack-nextcloud-data": NextcloudResourceRecord(
                resource_id="volume:wizard-stack-nextcloud-data",
                resource_name="wizard-stack-nextcloud-data",
            ),
            "wizard-stack-onlyoffice-data": NextcloudResourceRecord(
                resource_id="volume:wizard-stack-onlyoffice-data",
                resource_name="wizard-stack-onlyoffice-data",
            ),
        },
    )

    phase = reconcile_nextcloud(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=NEXTCLOUD_SERVICE_RESOURCE_TYPE,
                    resource_id="service:wizard-stack-nextcloud",
                    scope="stack:wizard-stack:nextcloud-service",
                ),
                OwnedResource(
                    resource_type=ONLYOFFICE_SERVICE_RESOURCE_TYPE,
                    resource_id="service:wizard-stack-onlyoffice",
                    scope="stack:wizard-stack:onlyoffice-service",
                ),
                OwnedResource(
                    resource_type=NEXTCLOUD_VOLUME_RESOURCE_TYPE,
                    resource_id="volume:wizard-stack-nextcloud-data",
                    scope="stack:wizard-stack:nextcloud-volume",
                ),
                OwnedResource(
                    resource_type=ONLYOFFICE_VOLUME_RESOURCE_TYPE,
                    resource_id="volume:wizard-stack-onlyoffice-data",
                    scope="stack:wizard-stack:onlyoffice-volume",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.nextcloud is not None
    assert phase.result.nextcloud.service.action == "update_owned"
    assert phase.result.nextcloud.data_volume.action == "reuse_owned"
    assert phase.result.onlyoffice is not None
    assert phase.result.onlyoffice.service.action == "update_owned"
    assert phase.result.onlyoffice.data_volume.action == "reuse_owned"
    assert phase.result.talk is not None
    assert phase.result.talk.enabled is True
    assert backend.create_service_calls == 0
    assert backend.update_service_calls == 2
    assert backend.create_volume_calls == 0


def test_reconcile_nextcloud_fails_closed_without_required_shared_core_allocation() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    desired_state = desired_state.__class__(
        format_version=desired_state.format_version,
        stack_name=desired_state.stack_name,
        root_domain=desired_state.root_domain,
        dokploy_url=desired_state.dokploy_url,
        dokploy_api_url=desired_state.dokploy_api_url,
        enable_tailscale=desired_state.enable_tailscale,
        tailscale_hostname=desired_state.tailscale_hostname,
        tailscale_enable_ssh=desired_state.tailscale_enable_ssh,
        tailscale_tags=desired_state.tailscale_tags,
        tailscale_subnet_routes=desired_state.tailscale_subnet_routes,
        cloudflare_access_otp_emails=desired_state.cloudflare_access_otp_emails,
        enabled_features=desired_state.enabled_features,
        selected_packs=desired_state.selected_packs,
        enabled_packs=desired_state.enabled_packs,
        hostnames=desired_state.hostnames,
        seaweedfs_access_key=desired_state.seaweedfs_access_key,
        seaweedfs_secret_key=desired_state.seaweedfs_secret_key,
        openclaw_gateway_token=desired_state.openclaw_gateway_token,
        openclaw_channels=desired_state.openclaw_channels,
        openclaw_replicas=desired_state.openclaw_replicas,
        my_farm_advisor_channels=desired_state.my_farm_advisor_channels,
        my_farm_advisor_replicas=desired_state.my_farm_advisor_replicas,
        shared_core=SharedCorePlan(
            network_name=desired_state.shared_core.network_name,
            postgres=desired_state.shared_core.postgres,
            redis=desired_state.shared_core.redis,
            allocations=(),
        ),
    )

    with pytest.raises(NextcloudError, match="pack_name 'nextcloud' is missing"):
        reconcile_nextcloud(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeNextcloudBackend(),
        )


def test_reconcile_nextcloud_fails_closed_on_unowned_collision() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = FakeNextcloudBackend(
        services={
            "wizard-stack-nextcloud": NextcloudResourceRecord(
                resource_id="service:collision",
                resource_name="wizard-stack-nextcloud",
            )
        }
    )

    with pytest.raises(NextcloudError, match="Refusing to adopt existing unowned service"):
        reconcile_nextcloud(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


def test_reconcile_nextcloud_reuses_existing_dokploy_managed_volumes() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = FakeNextcloudBackend(
        volumes={
            "wizard-stack-nextcloud-data": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:nextcloud-volume",
                resource_name="wizard-stack-nextcloud-data",
            ),
            "wizard-stack-onlyoffice-data": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:onlyoffice-volume",
                resource_name="wizard-stack-onlyoffice-data",
            ),
        }
    )

    phase = reconcile_nextcloud(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.nextcloud is not None
    assert phase.result.onlyoffice is not None
    assert phase.result.nextcloud.service.action == "create"
    assert phase.result.onlyoffice.service.action == "create"
    assert phase.result.nextcloud.data_volume.action == "reuse_existing"
    assert phase.result.onlyoffice.data_volume.action == "reuse_existing"


def test_reconcile_nextcloud_reuses_existing_dokploy_managed_services() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    backend = FakeNextcloudBackend(
        services={
            "wizard-stack-nextcloud": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:nextcloud-service",
                resource_name="wizard-stack-nextcloud",
            ),
            "wizard-stack-onlyoffice": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:onlyoffice-service",
                resource_name="wizard-stack-onlyoffice",
            ),
        },
        volumes={
            "wizard-stack-nextcloud-data": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:nextcloud-volume",
                resource_name="wizard-stack-nextcloud-data",
            ),
            "wizard-stack-onlyoffice-data": NextcloudResourceRecord(
                resource_id="dokploy-compose:cmp-existing:onlyoffice-volume",
                resource_name="wizard-stack-onlyoffice-data",
            ),
        },
    )

    phase = reconcile_nextcloud(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.nextcloud is not None
    assert phase.result.onlyoffice is not None
    assert phase.result.nextcloud.service.action == "reuse_existing"
    assert phase.result.onlyoffice.service.action == "reuse_existing"
    assert phase.result.nextcloud.data_volume.action == "reuse_existing"
    assert phase.result.onlyoffice.data_volume.action == "reuse_existing"
    assert backend.update_service_calls == 2


def test_reconcile_nextcloud_fails_when_onlyoffice_health_check_does_not_pass() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )

    with pytest.raises(NextcloudError, match="OnlyOffice health check failed"):
        reconcile_nextcloud(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeNextcloudBackend(health={"wizard-stack-onlyoffice": False}),
        )


def test_build_nextcloud_ledger_persists_only_pack_owned_resources() -> None:
    updated = build_nextcloud_ledger(
        existing_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="cloudflare_tunnel",
                    resource_id="tunnel-1",
                    scope="account:account-123",
                ),
            ),
        ),
        stack_name="wizard-stack",
        nextcloud_service_resource_id="service:wizard-stack-nextcloud",
        onlyoffice_service_resource_id="service:wizard-stack-onlyoffice",
        nextcloud_volume_resource_id="volume:wizard-stack-nextcloud-data",
        onlyoffice_volume_resource_id="volume:wizard-stack-onlyoffice-data",
    )

    assert {(resource.resource_type, resource.scope) for resource in updated.resources} == {
        ("cloudflare_tunnel", "account:account-123"),
        (NEXTCLOUD_SERVICE_RESOURCE_TYPE, "stack:wizard-stack:nextcloud-service"),
        (ONLYOFFICE_SERVICE_RESOURCE_TYPE, "stack:wizard-stack:onlyoffice-service"),
        (NEXTCLOUD_VOLUME_RESOURCE_TYPE, "stack:wizard-stack:nextcloud-volume"),
        (ONLYOFFICE_VOLUME_RESOURCE_TYPE, "stack:wizard-stack:onlyoffice-volume"),
    }


def test_dokploy_nextcloud_backend_creates_one_compose_for_pair() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        client=client,
    )

    nextcloud_volume = backend.create_volume(resource_name="wizard-stack-nextcloud-data")
    onlyoffice_volume = backend.create_volume(resource_name="wizard-stack-onlyoffice-data")
    nextcloud_service = backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )
    onlyoffice_service = backend.create_service(
        resource_name="wizard-stack-onlyoffice",
        hostname="office.example.com",
        data_volume_name="wizard-stack-onlyoffice-data",
        config={
            "integration_secret_ref": "wizard-stack-nextcloud-onlyoffice-jwt-secret",
            "nextcloud_url": "https://nextcloud.example.com",
        },
    )

    assert nextcloud_volume.resource_id == "dokploy-compose:cmp-1:nextcloud-volume"
    assert onlyoffice_volume.resource_id == "dokploy-compose:cmp-1:onlyoffice-volume"
    assert nextcloud_service.resource_id == "dokploy-compose:cmp-1:nextcloud-service"
    assert onlyoffice_service.resource_id == "dokploy-compose:cmp-1:onlyoffice-service"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    compose = client.last_create_compose_file
    assert compose is not None
    assert (
        'traefik.http.routers.wizard-stack-nextcloud.rule: "Host(`nextcloud.example.com`)"'
        in compose
    )
    assert "TRUSTED_PROXIES: 127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" in compose
    assert "OVERWRITECLIURL: https://nextcloud.example.com" in compose
    assert "NEXTCLOUD_ADMIN_USER: admin" in compose
    assert "NEXTCLOUD_ADMIN_PASSWORD: ChangeMeSoon" in compose
    assert "POSTGRES_PASSWORD: change-me" in compose
    assert "REDIS_HOST_PASSWORD: change-me" in compose
    assert 'traefik.http.services.wizard-stack-nextcloud.loadbalancer.server.port: "80"' in compose
    assert (
        'traefik.http.routers.wizard-stack-onlyoffice.rule: "Host(`office.example.com`)"' in compose
    )
    assert 'traefik.http.services.wizard-stack-onlyoffice.loadbalancer.server.port: "80"' in compose


def test_dokploy_nextcloud_backend_mounts_openclaw_volume_when_enabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        openclaw_volume_name="wizard-stack-openclaw-data",
        client=client,
    )

    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )

    compose = client.last_create_compose_file
    assert compose is not None
    assert "wizard-stack-openclaw-data:/mnt/openclaw" in compose
    assert "  wizard-stack-openclaw-data:" in compose
    assert "    name: wizard-stack-openclaw-data" in compose


def test_dokploy_nextcloud_backend_marks_nexa_workspace_as_operator_surface() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        openclaw_volume_name="wizard-stack-openclaw-data",
        openclaw_workspace_contract=NextcloudOpenClawWorkspaceContract(
            enabled=True,
            external_mount_name="/OpenClaw",
            external_mount_path="/mnt/openclaw",
            visible_root="/mnt/openclaw/workspace/nexa",
            contract_path="/mnt/openclaw/workspace/nexa/contract.json",
            runtime_state_source="server-owned env + durable state JSON",
            notes=("Nextcloud exposes the Nexa workspace as an operator/user surface only.",),
        ),
        client=client,
    )

    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )

    compose = client.last_create_compose_file
    assert compose is not None
    assert "wizard-stack-openclaw-data:/mnt/openclaw" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_STORAGE_MODE: operator-surface" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_NEXA_VISIBLE_ROOT: /mnt/openclaw/workspace/nexa" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_NEXA_CONTRACT_PATH: /mnt/openclaw/workspace/nexa/contract.json" in compose
    assert (
        "DOKPLOY_WIZARD_OPENCLAW_NEXA_RUNTIME_STATE_SOURCE: server-owned env + durable state JSON"
        in compose
    )


def test_build_nextcloud_backend_passes_nexa_service_account_inputs() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_API_KEY": "dokp-key-123",
            "ENABLE_NEXTCLOUD": "true",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
            "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
            "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD": "fallback-secret",
            "OPENCLAW_NEXA_AGENT_EMAIL": "nexa@example.com",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    backend = _build_nextcloud_backend(raw_env=raw_env, desired_state=desired_state)

    assert isinstance(backend, DokployNextcloudBackend)
    assert backend._nexa_agent_user_id == "nexa-agent"
    assert backend._nexa_agent_display_name == "Nexa"
    assert backend._nexa_agent_password == "fallback-secret"
    assert backend._nexa_agent_email == "nexa@example.com"


def test_build_nextcloud_backend_prefers_explicit_nexa_agent_password() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_API_KEY": "dokp-key-123",
            "ENABLE_NEXTCLOUD": "true",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
            "OPENCLAW_NEXA_AGENT_PASSWORD": "agent-secret",
            "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD": "fallback-secret",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    backend = _build_nextcloud_backend(raw_env=raw_env, desired_state=desired_state)

    assert isinstance(backend, DokployNextcloudBackend)
    assert backend._nexa_agent_password == "agent-secret"


def test_dokploy_nextcloud_backend_creates_openclaw_rescan_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        admin_user="clayton@example.com",
        openclaw_volume_name="wizard-stack-openclaw-data",
        client=client,
    )

    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )
    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_files_external_app", lambda container_name: None)
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_openclaw_external_storage",
        lambda container_name, *, admin_user: None,
    )

    backend.refresh_openclaw_external_storage(admin_user="clayton@example.com")

    assert client.schedules == [
        DokployScheduleRecord(
            schedule_id="sch-1",
            name="wizard-stack-openclaw-rescan",
            service_name="wizard-stack-nextcloud",
            cron_expression="*/15 * * * *",
            timezone="UTC",
            shell_type="bash",
            command='php /var/www/html/occ files:scan --path="clayton@example.com/files/OpenClaw"',
            enabled=True,
        )
    ]


def test_dokploy_nextcloud_backend_fails_on_openclaw_rescan_schedule_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient(schedule_auth_error="Dokploy API request failed with status 401: Unauthorized.")
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        admin_user="clayton@example.com",
        openclaw_volume_name="wizard-stack-openclaw-data",
        client=client,
    )

    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )
    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_files_external_app", lambda container_name: None)
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_openclaw_external_storage",
        lambda container_name, *, admin_user: None,
    )

    with pytest.raises(DokployApiError, match="401"):
        backend.refresh_openclaw_external_storage(admin_user="clayton@example.com")


def test_dokploy_nextcloud_backend_still_fails_on_non_auth_schedule_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient(schedule_auth_error="Dokploy API request failed with status 500: Internal Server Error.")
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        admin_user="clayton@example.com",
        openclaw_volume_name="wizard-stack-openclaw-data",
        client=client,
    )

    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": allocation.postgres.database_name,
            "postgres_password_secret_ref": allocation.postgres.password_secret_ref,
            "postgres_user_name": allocation.postgres.user_name,
            "redis_identity_name": allocation.redis.identity_name,
            "redis_password_secret_ref": allocation.redis.password_secret_ref,
        },
    )
    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_files_external_app", lambda container_name: None)
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_openclaw_external_storage",
        lambda container_name, *, admin_user: None,
    )

    with pytest.raises(DokployApiError, match="500"):
        backend.refresh_openclaw_external_storage(admin_user="clayton@example.com")


def test_reconcile_nextcloud_surfaces_talk_verification_runtime() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )

    phase = reconcile_nextcloud(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeNextcloudBackend(),
    )

    assert phase.result.talk is not None
    assert phase.result.talk.app_id == "spreed"
    assert phase.result.talk.enabled is True
    assert phase.result.talk.enabled_check.command == "php occ app:list --output=json"


def test_dokploy_nextcloud_backend_updates_existing_compose_to_keep_onlyoffice_route_managed() -> (
    None
):
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    existing_project = DokployProjectSummary(
        project_id="proj-1",
        name="wizard-stack",
        environments=(
            DokployEnvironmentSummary(
                environment_id="env-1",
                name="production",
                is_default=True,
                composes=(
                    DokployComposeSummary(
                        compose_id="cmp-existing",
                        name="wizard-stack-nextcloud",
                        status="done",
                    ),
                ),
            ),
        ),
    )
    client = FakeDokployApiClient(projects=[existing_project])
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        client=client,
    )

    record = backend.create_service(
        resource_name="wizard-stack-onlyoffice",
        hostname="office.example.com",
        data_volume_name="wizard-stack-onlyoffice-data",
        config={
            "integration_secret_ref": "wizard-stack-nextcloud-onlyoffice-jwt-secret",
            "nextcloud_url": "https://nextcloud.example.com",
        },
    )

    compose = client.last_update_compose_file
    assert record.resource_id == "dokploy-compose:cmp-existing:onlyoffice-service"
    assert compose is not None
    assert client.create_compose_calls == 0
    assert client.update_compose_calls == 1
    assert client.deploy_calls == 1
    assert "TRUSTED_PROXIES: 127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" in compose
    assert "OVERWRITECLIURL: https://nextcloud.example.com" in compose
    assert "POSTGRES_PASSWORD: change-me" in compose
    assert "REDIS_HOST_PASSWORD: change-me" in compose
    assert "JWT_SECRET: change-me" in compose
    assert "JWT_HEADER: Authorization" in compose
    assert "ALLOW_PRIVATE_IP_ADDRESS: 'true'" in compose
    assert "ALLOW_META_IP_ADDRESS: 'true'" in compose
    assert (
        'traefik.http.routers.wizard-stack-onlyoffice.middlewares: "wizard-stack-onlyoffice-forwarded-https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-onlyoffice-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-onlyoffice-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "office.example.com"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-onlyoffice-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"'
        in compose
    )
    assert (
        'traefik.http.routers.wizard-stack-onlyoffice.rule: "Host(`office.example.com`)"' in compose
    )
    assert 'traefik.http.services.wizard-stack-onlyoffice.loadbalancer.server.port: "80"' in compose


def test_local_https_health_check_uses_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str], bool]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(
        req: request.Request,
        timeout: int,
        context: ssl.SSLContext,
    ) -> FakeResponse:
        calls.append((req.full_url, dict(req.header_items()), context.check_hostname is False))
        return FakeResponse()

    monkeypatch.setattr("dokploy_wizard.dokploy.nextcloud.request.urlopen", fake_urlopen)

    assert _local_https_health_check("https://nextcloud.example.com/status.php") is True
    assert calls == [
        (
            "https://127.0.0.1/status.php",
            {"Host": "nextcloud.example.com"},
            True,
        )
    ]


def test_ensure_onlyoffice_app_config_sets_internal_urls_and_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_run_occ_shell(container_name: str, shell_command: str) -> None:
        assert container_name == "nextcloud-container"
        commands.append(shell_command)

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        fake_run_occ_shell,
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
    )

    assert commands == [
        "php occ app:enable --force onlyoffice",
        "php occ config:system:set allow_local_remote_servers --value=true --type=bool",
        "php occ config:system:set onlyoffice jwt_secret --value=change-me",
        "php occ config:system:set onlyoffice jwt_header --value=Authorization",
        "php occ config:app:set onlyoffice DocumentServerUrl --value=https://office.example.com",
        "php occ config:app:set onlyoffice DocumentServerInternalUrl --value=http://wizard-stack-onlyoffice",
        "php occ config:app:set onlyoffice StorageUrl --value=http://wizard-stack-nextcloud",
        "php occ config:app:set onlyoffice jwt_secret --value=change-me",
        'php occ config:app:set onlyoffice defFormats --value=\'{"docx":true,"docxf":true,"oform":true,"pdf":true,"pptx":true,"vsdx":true,"xlsx":true}\'',
        'php occ config:app:set onlyoffice editFormats --value=\'{"csv":true,"txt":true}\'',
        "php occ config:app:set onlyoffice sameTab --value=true",
        "php occ config:app:set onlyoffice preview --value=true",
        "php occ onlyoffice:documentserver --check",
    ]


def test_with_trailing_slash_adds_missing_separator() -> None:
    assert _with_trailing_slash("https://office.example.com") == "https://office.example.com/"
    assert (
        _with_trailing_slash("http://wizard-stack-onlyoffice/") == "http://wizard-stack-onlyoffice/"
    )


def test_www_data_occ_shell_uses_deterministic_php_binary_with_narrow_fallback() -> None:
    command = _www_data_occ_shell(
        "nextcloud-container",
        "php occ user:info 'clayton@example.com'",
    )

    assert command[:8] == [
        "docker",
        "exec",
        "nextcloud-container",
        "su",
        "-s",
        "/bin/sh",
        "www-data",
        "-c",
    ]
    shell_command = command[8]
    assert shell_command.startswith("cd /var/www/html && ")
    assert 'PHP_BIN=/usr/local/bin/php; ' in shell_command
    assert 'if [ ! -x "$PHP_BIN" ] && [ -x /usr/bin/php ]; then PHP_BIN=/usr/bin/php; fi; ' in shell_command
    assert 'echo "php not found at /usr/local/bin/php or /usr/bin/php" >&2; exit 127; ' in shell_command
    assert shell_command.endswith('"$PHP_BIN" occ user:info \'clayton@example.com\'')


def test_ensure_nexa_service_account_creates_user_and_updates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_run(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "user not found"})()

    monkeypatch.setattr(nextcloud_module.subprocess, "run", fake_run)
    monkeypatch.setattr(nextcloud_module, "_run_occ_shell", lambda container_name, shell_command: commands.append(shell_command))

    nextcloud_module._ensure_nexa_service_account(
        "nextcloud-container",
        user_id="nexa-agent",
        password="nexa-secret",
        display_name="Nexa",
        email="nexa@example.com",
    )

    assert "php occ user:add --password-from-env --display-name=Nexa nexa-agent" in commands[0]
    assert commands[1] == "php occ user:setting nexa-agent settings display_name Nexa"
    assert commands[2] == "php occ user:setting nexa-agent settings email nexa@example.com"
    assert commands[3] == "php occ user:profile nexa-agent profile_enabled 1"


def test_ensure_nexa_service_account_updates_existing_user_without_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_run(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return type("Result", (), {"returncode": 0, "stdout": "uid: nexa-agent", "stderr": ""})()

    monkeypatch.setattr(nextcloud_module.subprocess, "run", fake_run)
    monkeypatch.setattr(nextcloud_module, "_run_occ_shell", lambda container_name, shell_command: commands.append(shell_command))

    nextcloud_module._ensure_nexa_service_account(
        "nextcloud-container",
        user_id="nexa-agent",
        password="nexa-secret",
        display_name="Nexa",
        email=None,
    )

    assert len(commands) == 2
    assert commands[0] == "php occ user:setting nexa-agent settings display_name Nexa"
    assert commands[1] == "php occ user:profile nexa-agent profile_enabled 1"


def test_ensure_trusted_domain_adds_internal_service_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._read_occ_output",
        lambda container_name, args: "localhost\nnextcloud.example.com\n",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        lambda container_name, shell_command: commands.append(shell_command),
    )

    _ensure_trusted_domain("nextcloud-container", "wizard-stack-nextcloud")

    assert commands == [
        "php occ config:system:set trusted_domains 2 --value=wizard-stack-nextcloud"
    ]


def test_ensure_trusted_domain_skips_existing_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._read_occ_output",
        lambda container_name, args: "localhost\nwizard-stack-nextcloud\nnextcloud.example.com\n",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        lambda container_name, shell_command: commands.append(shell_command),
    )

    _ensure_trusted_domain("nextcloud-container", "wizard-stack-nextcloud")

    assert commands == []


def test_nextcloud_status_ready_requires_installed_true(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload: str) -> None:
            self._payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return self._payload.encode("utf-8")

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud.request.urlopen",
        lambda req, timeout, context: FakeResponse('{"installed": false, "maintenance": false}'),
    )
    assert _nextcloud_status_ready("https://nextcloud.example.com/status.php") is False

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud.request.urlopen",
        lambda req, timeout, context: FakeResponse('{"installed": true, "maintenance": false}'),
    )
    assert _nextcloud_status_ready("https://nextcloud.example.com/status.php") is True
