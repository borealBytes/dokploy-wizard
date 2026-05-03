# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import ssl
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib import request

import pytest

import dokploy_wizard.dokploy.nextcloud as nextcloud_module
import dokploy_wizard.packs.nextcloud as nextcloud_pack
from dokploy_wizard.core.models import (
    SharedCorePlan,
    SharedPostgresAllocation,
    SharedRedisAllocation,
)
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployNextcloudBackend,
    DokployProjectSummary,
    DokployScheduleRecord,
)
from dokploy_wizard.dokploy.nextcloud import (
    _ensure_nexa_service_account,
    _ensure_onlyoffice_app_config,
    _ensure_spreed_app_enabled,
    _ensure_trusted_domain,
    _find_container_name,
    _local_https_health_check,
    _nextcloud_status_ready,
    _platform_version_spec_matches_major,
    _resolve_compatible_app_release_download_url,
    _talk_app_enabled,
    _with_trailing_slash,
)
from dokploy_wizard.packs.nextcloud import (
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
    NextcloudAdvisorWorkspaceMountContract,
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    NextcloudError,
    NextcloudOpenClawWorkspaceContract,
    NextcloudResourceRecord,
    TalkRuntime,
    build_nextcloud_ledger,
    reconcile_nextcloud,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeNextcloudBackend:
    services: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    volumes: dict[str, NextcloudResourceRecord] = field(default_factory=dict)
    health: dict[str, bool] = field(default_factory=dict)
    create_service_calls: int = 0
    update_service_calls: int = 0
    create_volume_calls: int = 0
    refresh_calls: list[str] = field(default_factory=list)

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
        self.refresh_calls.append(admin_user)


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0
    schedules: list[DokployScheduleRecord] = field(default_factory=list)
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
        record = DokployScheduleRecord(
            schedule_id=f"sch-{len(self.schedules) + 1}",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules.append(record)
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
        self.schedules = [
            record if item.schedule_id == schedule_id else item for item in self.schedules
        ]
        return record

    def delete_schedule(self, *, schedule_id: str) -> None:
        self.schedules = [item for item in self.schedules if item.schedule_id != schedule_id]


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
    assert phase.result.onlyoffice.document_server_check.passed is None
    assert phase.result.talk is not None
    assert phase.result.talk.app_id == "spreed"
    assert phase.result.talk.enabled is None
    assert phase.result.talk.enabled_check.command == "php occ app:list --output=json"
    assert phase.result.talk.signaling_check.passed is None
    assert phase.result.talk.stun_check.passed is None
    assert phase.result.talk.turn_check.passed is None


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
    assert phase.result.talk is not None
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
    assert phase.result.talk is not None
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


def test_reconcile_nextcloud_fails_when_talk_is_not_enabled() -> None:
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

    class TalkDisabledBackend(FakeNextcloudBackend):
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
                    enabled=False,
                    enabled_check=NextcloudCommandCheck(
                        command="php occ app:list --output=json",
                        passed=False,
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

    with pytest.raises(NextcloudError, match="Talk app 'spreed' is not enabled"):
        reconcile_nextcloud(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=TalkDisabledBackend(),
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


def test_ensure_nexa_service_account_creates_user_and_updates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_run(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "user not found"})()

    monkeypatch.setattr(nextcloud_module.subprocess, "run", fake_run)
    monkeypatch.setattr(nextcloud_module, "_run_occ_shell", lambda container_name, shell_command: commands.append(shell_command))

    _ensure_nexa_service_account(
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

    _ensure_nexa_service_account(
        "nextcloud-container",
        user_id="nexa-agent",
        password="nexa-secret",
        display_name="Nexa",
        email=None,
    )

    assert len(commands) == 2
    assert commands[0] == "php occ user:setting nexa-agent settings display_name Nexa"
    assert commands[1] == "php occ user:profile nexa-agent profile_enabled 1"


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


def _openclaw_advisor_workspace_mount() -> NextcloudAdvisorWorkspaceMountContract:
    return NextcloudAdvisorWorkspaceMountContract(
        advisor_id="openclaw",
        volume_name="wizard-stack-openclaw-data",
        container_mount_root="/mnt/openclaw",
        external_mount_name="/OpenClaw",
        external_mount_path="/mnt/openclaw/workspace",
        visible_root="/mnt/openclaw/workspace/nexa",
        contract_path="/mnt/openclaw/workspace/nexa/contract.json",
        runtime_state_source="server-owned env + durable state JSON",
        rescan_schedule_identity="openclaw",
        notes=("Nextcloud exposes the Nexa workspace as an operator/user surface only.",),
    )


def _my_farm_advisor_workspace_mounts() -> tuple[NextcloudAdvisorWorkspaceMountContract, ...]:
    return (
        NextcloudAdvisorWorkspaceMountContract(
            advisor_id="my-farm-advisor",
            volume_name="wizard-stack-my-farm-advisor-data",
            container_mount_root="/mnt/my-farm-advisor",
            external_mount_name="/Nexa Farm",
            external_mount_path="/mnt/my-farm-advisor/field-operations",
            visible_root="/mnt/my-farm-advisor/field-operations/workspace",
            contract_path=None,
            runtime_state_source="wizard-managed service workspace",
            rescan_schedule_identity="my-farm-advisor-field-operations",
            notes=("Field operations workspace.",),
        ),
        NextcloudAdvisorWorkspaceMountContract(
            advisor_id="my-farm-advisor",
            volume_name="wizard-stack-my-farm-advisor-data",
            container_mount_root="/mnt/my-farm-advisor",
            external_mount_name="/Nexa Farm Data Pipeline",
            external_mount_path="/mnt/my-farm-advisor/data-pipeline",
            visible_root="/mnt/my-farm-advisor/data-pipeline/workspace",
            contract_path=None,
            runtime_state_source="wizard-managed data pipeline workspace",
            rescan_schedule_identity="my-farm-advisor-data-pipeline",
            notes=("Data pipeline workspace.",),
        ),
    )


def _all_advisor_workspace_mounts() -> tuple[NextcloudAdvisorWorkspaceMountContract, ...]:
    return (_openclaw_advisor_workspace_mount(), *_my_farm_advisor_workspace_mounts())


def _build_nextcloud_backend_for_mounts(
    *,
    advisor_workspace_mounts: tuple[NextcloudAdvisorWorkspaceMountContract, ...],
    client: FakeDokployApiClient | None = None,
    openclaw_rescan_cron: str = "*/15 * * * *",
    openclaw_rescan_timezone: str = "UTC",
) -> DokployNextcloudBackend:
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
    return DokployNextcloudBackend(
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
        admin_user="clayton@superiorbyteworks.com",
        advisor_workspace_mounts=advisor_workspace_mounts,
        openclaw_rescan_cron=openclaw_rescan_cron,
        openclaw_rescan_timezone=openclaw_rescan_timezone,
        client=client or FakeDokployApiClient(),
    )


def _render_nextcloud_compose_for_mounts(
    *,
    advisor_workspace_mounts: tuple[NextcloudAdvisorWorkspaceMountContract, ...] = (),
    openclaw_volume_name: str | None = None,
    openclaw_workspace_contract: NextcloudOpenClawWorkspaceContract | None = None,
) -> str:
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
        advisor_workspace_mounts=advisor_workspace_mounts,
        openclaw_volume_name=openclaw_volume_name,
        openclaw_workspace_contract=openclaw_workspace_contract,
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
    return compose


def test_dokploy_nextcloud_backend_renders_openclaw_only_mounts() -> None:
    compose = _render_nextcloud_compose_for_mounts(
        advisor_workspace_mounts=(_openclaw_advisor_workspace_mount(),),
    )

    assert "wizard-stack-openclaw-data:/mnt/advisors/openclaw" in compose
    assert "wizard-stack-my-farm-advisor-data" not in compose
    assert "  wizard-stack-openclaw-data:" in compose
    assert "    name: wizard-stack-openclaw-data" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_STORAGE_MODE: operator-surface" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_MOUNT_NAME: /Nexa Claw" in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_NEXA_VISIBLE_ROOT: /mnt/advisors/openclaw/workspace/nexa" in compose
    assert (
        "DOKPLOY_WIZARD_OPENCLAW_NEXA_CONTRACT_PATH: "
        "/mnt/advisors/openclaw/workspace/nexa/contract.json" in compose
    )
    assert (
        "DOKPLOY_WIZARD_OPENCLAW_NEXA_RUNTIME_STATE_SOURCE: server-owned env + durable state JSON"
        in compose
    )
    assert (
        'DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MOUNTS_JSON: "[{\\"advisor_id\\":\\"openclaw\\"'
        in compose
    )


def test_dokploy_nextcloud_backend_renders_farm_only_mounts_without_openclaw() -> None:
    compose = _render_nextcloud_compose_for_mounts(
        advisor_workspace_mounts=_my_farm_advisor_workspace_mounts(),
    )

    assert "wizard-stack-openclaw-data:/mnt/advisors/openclaw" not in compose
    assert "wizard-stack-my-farm-advisor-data:/mnt/advisors/my-farm-advisor" in compose
    assert compose.count("wizard-stack-my-farm-advisor-data:/mnt/advisors/my-farm-advisor") == 1
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_STORAGE_MODE" not in compose
    assert "DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MY_FARM_ADVISOR_FIELD_OPERATIONS_EXTERNAL_MOUNT_NAME: /Nexa Farm" in compose
    assert (
        "DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MY_FARM_ADVISOR_DATA_PIPELINE_EXTERNAL_MOUNT_NAME: "
        "/Nexa Farm Data Pipeline" in compose
    )
    assert "/mnt/advisors/my-farm-advisor/field-operations/workspace" in compose
    assert "/mnt/advisors/my-farm-advisor/data-pipeline/workspace" in compose


def test_dokploy_nextcloud_backend_renders_openclaw_and_farm_mounts_together() -> None:
    compose = _render_nextcloud_compose_for_mounts(
        advisor_workspace_mounts=(
            _openclaw_advisor_workspace_mount(),
            *_my_farm_advisor_workspace_mounts(),
        ),
    )

    assert "wizard-stack-openclaw-data:/mnt/advisors/openclaw" in compose
    assert "wizard-stack-my-farm-advisor-data:/mnt/advisors/my-farm-advisor" in compose
    assert compose.count("wizard-stack-my-farm-advisor-data:/mnt/advisors/my-farm-advisor") == 1
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_MOUNT_NAME: /Nexa Claw" in compose
    assert "DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MY_FARM_ADVISOR_FIELD_OPERATIONS_EXTERNAL_MOUNT_NAME: /Nexa Farm" in compose
    assert (
        "DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MY_FARM_ADVISOR_DATA_PIPELINE_EXTERNAL_MOUNT_NAME: "
        "/Nexa Farm Data Pipeline" in compose
    )


def test_dokploy_nextcloud_backend_renders_no_advisor_mounts_when_none_enabled() -> None:
    compose = _render_nextcloud_compose_for_mounts()

    assert "/mnt/advisors/openclaw" not in compose
    assert "/mnt/advisors/my-farm-advisor" not in compose
    assert "DOKPLOY_WIZARD_ADVISOR_WORKSPACE_MOUNTS_JSON" not in compose
    assert "DOKPLOY_WIZARD_OPENCLAW_EXTERNAL_STORAGE_MODE" not in compose


def test_nextcloud_advisor_mount_contracts_can_coexist_without_path_collisions() -> None:
    contracts = (
        NextcloudAdvisorWorkspaceMountContract(
            advisor_id="openclaw",
            volume_name="wizard-stack-openclaw-data",
            container_mount_root="/mnt/openclaw",
            external_mount_name="/Nexa Claw",
            external_mount_path="/mnt/openclaw/workspace",
            visible_root="/mnt/openclaw/workspace/nexa",
            contract_path="/mnt/openclaw/workspace/nexa/contract.json",
            runtime_state_source="server-owned env + durable state JSON",
            rescan_schedule_identity="openclaw",
            notes=("Operator-facing Nexa workspace.",),
        ),
        NextcloudAdvisorWorkspaceMountContract(
            advisor_id="my-farm-advisor",
            volume_name="wizard-stack-my-farm-advisor-data",
            container_mount_root="/mnt/my-farm-advisor",
            external_mount_name="/Nexa Farm",
            external_mount_path="/mnt/my-farm-advisor/field-operations",
            visible_root="/mnt/my-farm-advisor/field-operations/workspace",
            contract_path=None,
            runtime_state_source="wizard-managed service workspace",
            rescan_schedule_identity="my-farm-advisor-field-operations",
            notes=("Field operations workspace.",),
        ),
        NextcloudAdvisorWorkspaceMountContract(
            advisor_id="my-farm-advisor",
            volume_name="wizard-stack-my-farm-advisor-data",
            container_mount_root="/mnt/my-farm-advisor",
            external_mount_name="/Nexa Farm Data Pipeline",
            external_mount_path="/mnt/my-farm-advisor/data-pipeline",
            visible_root="/mnt/my-farm-advisor/data-pipeline/workspace",
            contract_path=None,
            runtime_state_source="wizard-managed data pipeline workspace",
            rescan_schedule_identity="my-farm-advisor-data-pipeline",
            notes=("Data pipeline workspace.",),
        ),
    )

    assert len({item.external_mount_name for item in contracts}) == 3
    assert len({item.external_mount_path for item in contracts}) == 3
    assert {item.external_mount_name for item in contracts} == {
        "/Nexa Claw",
        "/Nexa Farm",
        "/Nexa Farm Data Pipeline",
    }


def test_nextcloud_advisor_workspace_contract_serialization_is_deterministic() -> None:
    contract = NextcloudAdvisorWorkspaceMountContract(
        advisor_id="openclaw",
        volume_name="wizard-stack-openclaw-data",
        container_mount_root="/mnt/openclaw",
        external_mount_name="/Nexa Claw",
        external_mount_path="/mnt/openclaw/workspace",
        visible_root="/mnt/openclaw/workspace/nexa",
        contract_path="/mnt/openclaw/workspace/nexa/contract.json",
        runtime_state_source="server-owned env + durable state JSON",
        rescan_schedule_identity="openclaw",
        notes=("Operator-facing Nexa workspace.",),
    )

    expected = {
        "advisor_id": "openclaw",
        "container_mount_root": "/mnt/openclaw",
        "contract_path": "/mnt/openclaw/workspace/nexa/contract.json",
        "enabled": True,
        "external_mount_name": "/Nexa Claw",
        "external_mount_path": "/mnt/openclaw/workspace",
        "notes": ["Operator-facing Nexa workspace."],
        "read_write_mode": True,
        "rescan_schedule_identity": "openclaw",
        "runtime_state_source": "server-owned env + durable state JSON",
        "visible_root": "/mnt/openclaw/workspace/nexa",
        "volume_name": "wizard-stack-openclaw-data",
    }

    assert contract.to_dict() == expected
    assert contract.to_dict() == expected


def test_nextcloud_openclaw_workspace_contract_import_remains_backward_compatible() -> None:
    contract = nextcloud_pack.NextcloudOpenClawWorkspaceContract(
        enabled=True,
        external_mount_name="/Nexa Claw",
        external_mount_path="/mnt/openclaw/workspace",
        visible_root="/mnt/openclaw/workspace/nexa",
        contract_path="/mnt/openclaw/workspace/nexa/contract.json",
        runtime_state_source="server-owned env + durable state JSON",
        notes=("Operator-facing Nexa workspace.",),
    )

    assert isinstance(contract, NextcloudOpenClawWorkspaceContract)
    assert contract.advisor_mount == NextcloudAdvisorWorkspaceMountContract(
        advisor_id="openclaw",
        volume_name="openclaw-data",
        container_mount_root="/mnt/openclaw",
        external_mount_name="/Nexa Claw",
        external_mount_path="/mnt/openclaw/workspace",
        visible_root="/mnt/openclaw/workspace/nexa",
        contract_path="/mnt/openclaw/workspace/nexa/contract.json",
        runtime_state_source="server-owned env + durable state JSON",
        rescan_schedule_identity="openclaw",
        notes=("Operator-facing Nexa workspace.",),
        enabled=True,
    )


def test_dokploy_nextcloud_backend_creates_openclaw_rescan_schedule() -> None:
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
        admin_user="clayton@superiorbyteworks.com",
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
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container"
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_files_external_app",
        lambda container_name: None,
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_advisor_external_storage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_find_external_storage_mount_id",
        lambda container_name, *, mount_point, datadir: None,
    )
    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")
    monkeypatch.undo()

    assert client.schedules == [
        DokployScheduleRecord(
            schedule_id="sch-1",
            name="wizard-stack-openclaw-rescan",
            service_name="wizard-stack-nextcloud",
            cron_expression="*/15 * * * *",
            timezone="UTC",
            shell_type="bash",
            command='php /var/www/html/occ files:scan --path="clayton@superiorbyteworks.com/files/Nexa Claw"',
            enabled=True,
        )
    ]


def test_dokploy_nextcloud_backend_updates_existing_openclaw_rescan_schedule() -> None:
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
    client = FakeDokployApiClient(
        schedules=[
            DokployScheduleRecord(
                schedule_id="sch-1",
                name="wizard-stack-openclaw-rescan",
                service_name="wizard-stack-nextcloud",
                cron_expression="0 * * * *",
                timezone="America/Detroit",
                shell_type="bash",
                command='php /var/www/html/occ files:scan --path="clayton@superiorbyteworks.com/files/Nexa Claw"',
                enabled=True,
            )
        ]
    )
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
        admin_user="clayton@superiorbyteworks.com",
        openclaw_volume_name="wizard-stack-openclaw-data",
        openclaw_rescan_cron="*/5 * * * *",
        openclaw_rescan_timezone="UTC",
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
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container"
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_files_external_app",
        lambda container_name: None,
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_ensure_advisor_external_storage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        nextcloud_module,
        "_find_external_storage_mount_id",
        lambda container_name, *, mount_point, datadir: None,
    )
    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")
    monkeypatch.undo()

    assert client.schedules == [
        DokployScheduleRecord(
            schedule_id="sch-1",
            name="wizard-stack-openclaw-rescan",
            service_name="wizard-stack-nextcloud",
            cron_expression="*/5 * * * *",
            timezone="UTC",
            shell_type="bash",
            command='php /var/www/html/occ files:scan --path="clayton@superiorbyteworks.com/files/Nexa Claw"',
            enabled=True,
        )
    ]


def test_dokploy_nextcloud_backend_refreshes_all_advisor_external_storages_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDokployApiClient()
    backend = _build_nextcloud_backend_for_mounts(
        advisor_workspace_mounts=_all_advisor_workspace_mounts(),
        client=client,
    )
    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": "wizard_stack_nextcloud",
            "postgres_password_secret_ref": "wizard-stack-nextcloud-postgres-password",
            "postgres_user_name": "wizard_stack_nextcloud",
            "redis_identity_name": "wizard-stack-nextcloud-redis",
            "redis_password_secret_ref": "wizard-stack-nextcloud-redis-password",
        },
    )
    mounts: list[dict[str, Any]] = []
    next_mount_id = 1
    commands: list[tuple[str, ...]] = []

    def fake_run_occ(container_name: str, args: list[str]) -> None:
        nonlocal next_mount_id
        assert container_name == "nextcloud-container"
        command = tuple(args)
        commands.append(command)
        if command[:2] == ("files_external:create", "/Nexa Claw"):
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Claw",
                    "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
                }
            )
            next_mount_id += 1
        elif command[:2] == ("files_external:create", "/Nexa Farm"):
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/field-operations"},
                }
            )
            next_mount_id += 1
        elif command[:2] == ("files_external:create", "/Nexa Farm Data Pipeline"):
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm Data Pipeline",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/data-pipeline"},
                }
            )
            next_mount_id += 1
        elif command[:2] == ("files_external:delete", "--yes"):
            mounts[:] = [item for item in mounts if str(item["mount_id"]) != command[2]]

    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_external_storage_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(nextcloud_module, "_list_external_storage_mounts", lambda _: tuple(mounts))
    monkeypatch.setattr(nextcloud_module, "_run_occ", fake_run_occ)

    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")
    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")

    assert mounts == [
        {
            "mount_id": 1,
            "mount_point": "/Nexa Claw",
            "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
        },
        {
            "mount_id": 2,
            "mount_point": "/Nexa Farm",
            "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/field-operations"},
        },
        {
            "mount_id": 3,
            "mount_point": "/Nexa Farm Data Pipeline",
            "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/data-pipeline"},
        },
    ]
    assert {item.name for item in client.schedules} == {
        "wizard-stack-openclaw-rescan",
        "wizard-stack-my-farm-advisor-field-operations-rescan",
        "wizard-stack-my-farm-advisor-data-pipeline-rescan",
    }
    assert client.schedules[0].command == (
        'php /var/www/html/occ files:scan '
        '--path="clayton@superiorbyteworks.com/files/Nexa Claw"'
    )
    assert (
        "files_external:create",
        "/Nexa Farm",
        "local",
        "null::null",
        "-c",
        "datadir=/mnt/advisors/my-farm-advisor/field-operations",
    ) in commands
    assert ("files_external:option", "2", "readonly", "false") in commands
    assert ("files_external:option", "3", "readonly", "false") in commands


def test_dokploy_nextcloud_backend_preserves_unrelated_user_created_external_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _build_nextcloud_backend_for_mounts(
        advisor_workspace_mounts=_all_advisor_workspace_mounts(),
    )
    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": "wizard_stack_nextcloud",
            "postgres_password_secret_ref": "wizard-stack-nextcloud-postgres-password",
            "postgres_user_name": "wizard_stack_nextcloud",
            "redis_identity_name": "wizard-stack-nextcloud-redis",
            "redis_password_secret_ref": "wizard-stack-nextcloud-redis-password",
        },
    )
    mounts: list[dict[str, Any]] = [
        {
            "mount_id": 91,
            "mount_point": "/Nexa Farm",
            "configuration": {"datadir": "/srv/user-created/nexa-farm"},
        }
    ]
    next_mount_id = 92

    def fake_run_occ(container_name: str, args: list[str]) -> None:
        nonlocal next_mount_id
        assert container_name == "nextcloud-container"
        if args[:2] == ["files_external:create", "/Nexa Claw"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Claw",
                    "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
                }
            )
            next_mount_id += 1
        elif args[:2] == ["files_external:create", "/Nexa Farm"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/field-operations"},
                }
            )
            next_mount_id += 1
        elif args[:2] == ["files_external:create", "/Nexa Farm Data Pipeline"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm Data Pipeline",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/data-pipeline"},
                }
            )
            next_mount_id += 1
        elif args[:2] == ["files_external:delete", "--yes"]:
            mounts[:] = [item for item in mounts if str(item["mount_id"]) != args[2]]

    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_external_storage_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(nextcloud_module, "_list_external_storage_mounts", lambda _: tuple(mounts))
    monkeypatch.setattr(nextcloud_module, "_run_occ", fake_run_occ)

    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")

    assert {
        (item["mount_point"], item["configuration"]["datadir"])
        for item in mounts
    } == {
        ("/Nexa Farm", "/srv/user-created/nexa-farm"),
        ("/Nexa Claw", "/mnt/advisors/openclaw/workspace"),
        ("/Nexa Farm", "/mnt/advisors/my-farm-advisor/field-operations"),
        ("/Nexa Farm Data Pipeline", "/mnt/advisors/my-farm-advisor/data-pipeline"),
    }


def test_dokploy_nextcloud_backend_replaces_stale_wizard_owned_mount_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _build_nextcloud_backend_for_mounts(
        advisor_workspace_mounts=_all_advisor_workspace_mounts(),
    )
    backend.create_service(
        resource_name="wizard-stack-nextcloud",
        hostname="nextcloud.example.com",
        data_volume_name="wizard-stack-nextcloud-data",
        config={
            "onlyoffice_url": "https://office.example.com",
            "postgres_database_name": "wizard_stack_nextcloud",
            "postgres_password_secret_ref": "wizard-stack-nextcloud-postgres-password",
            "postgres_user_name": "wizard_stack_nextcloud",
            "redis_identity_name": "wizard-stack-nextcloud-redis",
            "redis_password_secret_ref": "wizard-stack-nextcloud-redis-password",
        },
    )
    mounts: list[dict[str, Any]] = [
        {
            "mount_id": 7,
            "mount_point": "/OpenClaw",
            "configuration": {"datadir": "/mnt/openclaw"},
        },
        {
            "mount_id": 8,
            "mount_point": "/Nexa Farm",
            "configuration": {"datadir": "/mnt/my-farm-advisor/field-operations-old"},
        },
    ]
    next_mount_id = 9

    def fake_run_occ(container_name: str, args: list[str]) -> None:
        nonlocal next_mount_id
        assert container_name == "nextcloud-container"
        if args[:2] == ["files_external:delete", "--yes"]:
            mounts[:] = [item for item in mounts if str(item["mount_id"]) != args[2]]
            return
        if args[:2] == ["files_external:create", "/Nexa Claw"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Claw",
                    "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
                }
            )
        elif args[:2] == ["files_external:create", "/Nexa Farm"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/field-operations"},
                }
            )
        elif args[:2] == ["files_external:create", "/Nexa Farm Data Pipeline"]:
            mounts.append(
                {
                    "mount_id": next_mount_id,
                    "mount_point": "/Nexa Farm Data Pipeline",
                    "configuration": {"datadir": "/mnt/advisors/my-farm-advisor/data-pipeline"},
                }
            )
        else:
            return
        next_mount_id += 1

    monkeypatch.setattr(nextcloud_module, "_find_container_name", lambda service_name: "nextcloud-container")
    monkeypatch.setattr(nextcloud_module, "_ensure_external_storage_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(nextcloud_module, "_list_external_storage_mounts", lambda _: tuple(mounts))
    monkeypatch.setattr(nextcloud_module, "_run_occ", fake_run_occ)

    backend.refresh_openclaw_external_storage(admin_user="clayton@superiorbyteworks.com")

    assert {
        (item["mount_point"], item["configuration"]["datadir"])
        for item in mounts
    } == {
        ("/Nexa Claw", "/mnt/advisors/openclaw/workspace"),
        ("/Nexa Farm", "/mnt/advisors/my-farm-advisor/field-operations"),
        ("/Nexa Farm Data Pipeline", "/mnt/advisors/my-farm-advisor/data-pipeline"),
    }


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


def test_dokploy_nextcloud_onlyoffice_health_accepts_immediate_public_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        nextcloud_hostname="nextcloud.example.com",
        onlyoffice_hostname="office.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_nextcloud",
            user_name="wizard_stack_nextcloud",
            password_secret_ref="wizard-stack-nextcloud-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name="wizard-stack-nextcloud-redis",
            password_secret_ref="wizard-stack-nextcloud-redis-password",
        ),
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        client=FakeDokployApiClient(),
    )
    monkeypatch.setattr(nextcloud_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(nextcloud_module, "_public_https_health_check", lambda url: True)
    wait_calls: list[str] = []
    monkeypatch.setattr(
        nextcloud_module,
        "_wait_for_public_https_health",
        lambda url: wait_calls.append(url) or False,
    )

    ok = backend.check_health(
        service=NextcloudResourceRecord("onlyoffice-service-1", "wizard-stack-onlyoffice"),
        url="https://office.example.com/healthcheck",
    )

    assert ok is True
    assert wait_calls == []


def test_dokploy_nextcloud_onlyoffice_health_waits_for_public_route_on_first_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        nextcloud_hostname="nextcloud.example.com",
        onlyoffice_hostname="office.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_nextcloud",
            user_name="wizard_stack_nextcloud",
            password_secret_ref="wizard-stack-nextcloud-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name="wizard-stack-nextcloud-redis",
            password_secret_ref="wizard-stack-nextcloud-redis-password",
        ),
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        client=FakeDokployApiClient(),
    )
    backend._created_in_process = True
    monkeypatch.setattr(nextcloud_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(nextcloud_module, "_public_https_health_check", lambda url: False)
    waited_urls: list[str] = []
    monkeypatch.setattr(
        nextcloud_module,
        "_wait_for_public_https_health",
        lambda url: waited_urls.append(url) or True,
    )

    ok = backend.check_health(
        service=NextcloudResourceRecord("onlyoffice-service-1", "wizard-stack-onlyoffice"),
        url="https://office.example.com/healthcheck",
    )

    assert ok is True
    assert waited_urls == ["https://office.example.com/healthcheck"]


def test_dokploy_nextcloud_onlyoffice_health_fails_closed_without_first_apply_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        nextcloud_hostname="nextcloud.example.com",
        onlyoffice_hostname="office.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_nextcloud",
            user_name="wizard_stack_nextcloud",
            password_secret_ref="wizard-stack-nextcloud-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name="wizard-stack-nextcloud-redis",
            password_secret_ref="wizard-stack-nextcloud-redis-password",
        ),
        integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
        client=FakeDokployApiClient(),
    )
    monkeypatch.setattr(nextcloud_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(nextcloud_module, "_public_https_health_check", lambda url: False)
    wait_calls: list[str] = []
    monkeypatch.setattr(
        nextcloud_module,
        "_wait_for_public_https_health",
        lambda url: wait_calls.append(url) or True,
    )

    ok = backend.check_health(
        service=NextcloudResourceRecord("onlyoffice-service-1", "wizard-stack-onlyoffice"),
        url="https://office.example.com/healthcheck",
    )

    assert ok is False
    assert wait_calls == []


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


def test_ensure_onlyoffice_app_config_bootstraps_openclaw_external_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    new_mount_id_calls = 0

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        lambda container_name, shell_command: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ",
        lambda container_name, args: commands.append(tuple(args)),
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_external_storage_path",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._list_external_storage_mounts", lambda _: ()
    )

    def fake_find_mount_id(container_name: str, *, mount_point: str, datadir: str) -> str | None:
        nonlocal new_mount_id_calls
        assert datadir == "/mnt/advisors/openclaw/workspace"
        if mount_point == "/Nexa Claw":
            new_mount_id_calls += 1
            return None if new_mount_id_calls == 1 else "17"
        assert mount_point == "/OpenClaw"
        return None

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._find_external_storage_mount_id",
        fake_find_mount_id,
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
        openclaw_external_storage_enabled=True,
        admin_user="clayton@example.com",
    )

    assert ("app:enable", "files_external") in commands
    assert (
        "files_external:create",
        "/Nexa Claw",
        "local",
        "null::null",
        "-c",
        "datadir=/mnt/advisors/openclaw/workspace",
    ) in commands
    assert ("files_external:applicable", "17", "--add-user=clayton@example.com") in commands
    assert ("files_external:option", "17", "readonly", "false") in commands
    assert ("files_external:verify", "17") in commands
    assert ("files_external:scan", "17") in commands
    assert (
        "files:scan",
        "--path=clayton@example.com/files/Nexa Claw",
    ) in commands


def test_ensure_onlyoffice_app_config_reuses_legacy_openclaw_mount_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        lambda container_name, shell_command: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ",
        lambda container_name, args: commands.append(tuple(args)),
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_external_storage_path",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._list_external_storage_mounts",
        lambda _: (
            {
                "mount_id": 17,
                "mount_point": "/OpenClaw",
                "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
            },
        ),
    )

    def fake_find_mount_id(container_name: str, *, mount_point: str, datadir: str) -> str | None:
        assert datadir == "/mnt/advisors/openclaw/workspace"
        if mount_point == "/Nexa Claw":
            return None
        assert mount_point == "/OpenClaw"
        return "17"

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._find_external_storage_mount_id",
        fake_find_mount_id,
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
        openclaw_external_storage_enabled=True,
        admin_user="clayton@example.com",
    )

    assert not any(command[:2] == ("files_external:create", "/Nexa Claw") for command in commands)
    assert ("files_external:applicable", "17", "--add-user=clayton@example.com") in commands
    assert (
        "files:scan",
        "--path=clayton@example.com/files/OpenClaw",
    ) in commands


def test_ensure_onlyoffice_app_config_replaces_stale_openclaw_external_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    new_mount_id_calls = 0

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        lambda container_name, shell_command: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ",
        lambda container_name, args: commands.append(tuple(args)),
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_external_storage_path",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._list_external_storage_mounts",
        lambda _: (
            {
                "mount_id": 9,
                "mount_point": "/OpenClaw",
                "configuration": {"datadir": "/mnt/advisors/openclaw"},
            },
        ),
    )

    def fake_find_mount_id(container_name: str, *, mount_point: str, datadir: str) -> str | None:
        nonlocal new_mount_id_calls
        assert datadir == "/mnt/advisors/openclaw/workspace"
        if mount_point == "/Nexa Claw":
            new_mount_id_calls += 1
            return None if new_mount_id_calls == 1 else "17"
        assert mount_point == "/OpenClaw"
        return None

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._find_external_storage_mount_id",
        fake_find_mount_id,
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
        openclaw_external_storage_enabled=True,
        admin_user="clayton@example.com",
    )

    assert commands.index(("files_external:delete", "--yes", "9")) < commands.index(
        (
            "files_external:create",
            "/Nexa Claw",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/openclaw/workspace",
        )
    )


def test_ensure_onlyoffice_app_config_waits_for_transient_documentserver_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    documentserver_attempts = 0
    sleep_calls: list[float] = []

    def fake_run_occ_shell(container_name: str, shell_command: str) -> None:
        nonlocal documentserver_attempts
        assert container_name == "nextcloud-container"
        commands.append(shell_command)
        if shell_command == "php occ onlyoffice:documentserver --check":
            documentserver_attempts += 1
            if documentserver_attempts < 3:
                raise NextcloudError(
                    "Nextcloud OCC command failed (php occ onlyoffice:documentserver --check): 502 Bad Gateway"
                )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        fake_run_occ_shell,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
        wait_for_documentserver_check=True,
    )

    assert commands[-3:] == [
        "php occ onlyoffice:documentserver --check",
        "php occ onlyoffice:documentserver --check",
        "php occ onlyoffice:documentserver --check",
    ]
    assert sleep_calls == [5.0, 5.0]


def test_ensure_onlyoffice_app_config_fails_closed_after_documentserver_warmup_exhausts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documentserver_attempts = 0
    sleep_calls: list[float] = []

    def fake_run_occ_shell(container_name: str, shell_command: str) -> None:
        nonlocal documentserver_attempts
        assert container_name == "nextcloud-container"
        if shell_command == "php occ onlyoffice:documentserver --check":
            documentserver_attempts += 1
            raise NextcloudError(
                "Nextcloud OCC command failed (php occ onlyoffice:documentserver --check): 502 Bad Gateway"
            )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        fake_run_occ_shell,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    with pytest.raises(NextcloudError, match="502 Bad Gateway"):
        _ensure_onlyoffice_app_config(
            "nextcloud-container",
            document_server_url="https://office.example.com",
            document_server_internal_url="http://wizard-stack-onlyoffice",
            storage_url="http://wizard-stack-nextcloud",
            jwt_secret="change-me",
            wait_for_documentserver_check=True,
        )

    assert documentserver_attempts == 180
    assert sleep_calls == [5.0] * 179


def test_platform_version_spec_matches_major_handles_compound_constraints() -> None:
    assert _platform_version_spec_matches_major(">=33.0.0 <34.0.0", 33) is True
    assert _platform_version_spec_matches_major(">=33.0.0 <34.0.0", 32) is False
    assert _platform_version_spec_matches_major(">=33.0.0 <34.0.0", 34) is False


def test_resolve_compatible_app_release_download_url_matches_nextcloud_major(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, payload: str) -> None:
            self._payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return self._payload.encode("utf-8")

    requested_urls: list[str] = []
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._read_occ_www_data_output",
        lambda container_name, args: '{"versionstring":"33.0.2"}',
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud.request.urlopen",
        lambda req, timeout: (
            requested_urls.append(req.full_url)
            or FakeResponse(
                """[
                {"id":"spreed","releases":[
                    {"version":"9.0.9","platformVersionSpec":">=9.0.0 <10.0.0","download":"https://github.com/nextcloud/spreed/releases/download/v9.0.9/spreed-9.0.9.tar.gz"},
                    {"version":"23.0.3","platformVersionSpec":">=33.0.0 <34.0.0","download":"https://github.com/nextcloud-releases/spreed/releases/download/v23.0.3/spreed-v23.0.3.tar.gz"}
                ]},
                {"id":"onlyoffice","releases":[
                    {"version":"9.9.0","platformVersionSpec":">=9.0.0 <10.0.0","download":"https://github.com/ONLYOFFICE/onlyoffice-nextcloud/releases/download/v9.9.0/onlyoffice.tar.gz"},
                    {"version":"10.0.0","platformVersionSpec":">=33.0.0 <34.0.0","download":"https://github.com/ONLYOFFICE/onlyoffice-nextcloud/releases/download/v10.0.0/onlyoffice.tar.gz"}
                ]}
                ]"""
            )
        ),
    )

    assert _resolve_compatible_app_release_download_url("nextcloud-container", "spreed") == (
        "https://github.com/nextcloud-releases/spreed/releases/download/v23.0.3/spreed-v23.0.3.tar.gz"
    )
    assert _resolve_compatible_app_release_download_url("nextcloud-container", "onlyoffice") == (
        "https://github.com/ONLYOFFICE/onlyoffice-nextcloud/releases/download/v10.0.0/onlyoffice.tar.gz"
    )
    assert requested_urls == [
        "https://apps.nextcloud.com/api/v1/apps.json",
        "https://apps.nextcloud.com/api/v1/apps.json",
    ]


def test_ensure_onlyoffice_app_config_falls_back_to_manual_release_install_when_enable_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    enable_attempts = 0

    def fake_run_occ_shell(container_name: str, shell_command: str) -> None:
        nonlocal enable_attempts
        assert container_name == "nextcloud-container"
        commands.append(shell_command)
        if shell_command == "php occ app:enable --force onlyoffice" and enable_attempts == 0:
            enable_attempts += 1
            raise NextcloudError(
                "Nextcloud OCC command failed (php occ app:enable --force onlyoffice): onlyoffice is not installed"
            )
        if shell_command == "php occ app:enable --force onlyoffice":
            enable_attempts += 1

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        fake_run_occ_shell,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._resolve_compatible_app_release_download_url",
        lambda container_name,
        app_id: "https://github.com/ONLYOFFICE/onlyoffice-nextcloud/releases/download/v9.9.0/onlyoffice.tar.gz",
    )

    _ensure_onlyoffice_app_config(
        "nextcloud-container",
        document_server_url="https://office.example.com",
        document_server_internal_url="http://wizard-stack-onlyoffice",
        storage_url="http://wizard-stack-nextcloud",
        jwt_secret="change-me",
    )

    assert commands[0:3] == [
        "php occ app:enable --force onlyoffice",
        'export NEXTCLOUD_APP_TMP_DIR="$(mktemp -d)" && '
        "trap 'rm -rf \"$NEXTCLOUD_APP_TMP_DIR\"' EXIT && "
        'php -r \'if (!copy("https://github.com/ONLYOFFICE/onlyoffice-nextcloud/releases/download/v9.9.0/onlyoffice.tar.gz", getenv("NEXTCLOUD_APP_TMP_DIR") . "/app-release.tar.gz")) { fwrite(STDERR, "Failed to download ONLYOFFICE app release\\n"); exit(1); }\' && '
        "rm -rf apps/onlyoffice && "
        'tar -xzf "$NEXTCLOUD_APP_TMP_DIR/app-release.tar.gz" -C apps && '
        "test -d apps/onlyoffice",
        "php occ app:enable --force onlyoffice",
    ]
    assert commands[-1] == "php occ onlyoffice:documentserver --check"
    assert enable_attempts == 2


def test_ensure_spreed_app_enabled_keeps_happy_path_minimal(
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

    _ensure_spreed_app_enabled("nextcloud-container")

    assert commands == ["php occ app:enable spreed"]


def test_ensure_spreed_app_enabled_falls_back_to_manual_release_install_when_enable_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    enable_attempts = 0

    def fake_run_occ_shell(container_name: str, shell_command: str) -> None:
        nonlocal enable_attempts
        assert container_name == "nextcloud-container"
        commands.append(shell_command)
        if shell_command == "php occ app:enable spreed" and enable_attempts == 0:
            enable_attempts += 1
            raise NextcloudError(
                "Nextcloud OCC command failed (php occ app:enable spreed): Could not download app spreed, it was not found on the appstore"
            )
        if shell_command == "php occ app:enable spreed":
            enable_attempts += 1

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._run_occ_shell",
        fake_run_occ_shell,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._resolve_compatible_app_release_download_url",
        lambda container_name,
        app_id: "https://github.com/nextcloud/spreed/releases/download/v21.0.0/spreed-21.0.0.tar.gz",
    )

    _ensure_spreed_app_enabled("nextcloud-container")

    assert commands == [
        "php occ app:enable spreed",
        'export NEXTCLOUD_APP_TMP_DIR="$(mktemp -d)" && '
        "trap 'rm -rf \"$NEXTCLOUD_APP_TMP_DIR\"' EXIT && "
        'php -r \'if (!copy("https://github.com/nextcloud/spreed/releases/download/v21.0.0/spreed-21.0.0.tar.gz", getenv("NEXTCLOUD_APP_TMP_DIR") . "/app-release.tar.gz")) { fwrite(STDERR, "Failed to download Talk app release\\n"); exit(1); }\' && '
        "rm -rf apps/spreed && "
        'tar -xzf "$NEXTCLOUD_APP_TMP_DIR/app-release.tar.gz" -C apps && '
        "test -d apps/spreed",
        "php occ app:enable spreed",
    ]
    assert enable_attempts == 2


def test_talk_app_enabled_accepts_real_object_shaped_enabled_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._read_occ_www_data_output",
        lambda container_name, args: (
            '{"enabled":{"onlyoffice":"10.0.0","spreed":"23.0.3"},"disabled":{"files_pdfviewer":"3.1.0"}}'
        ),
    )

    assert _talk_app_enabled("nextcloud-container") is True


def test_talk_app_enabled_rejects_missing_enabled_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._read_occ_www_data_output",
        lambda container_name, args: '{"disabled":{"spreed":"23.0.3"}}',
    )

    with pytest.raises(NextcloudError, match="enabled app collection"):
        _talk_app_enabled("nextcloud-container")


def test_with_trailing_slash_adds_missing_separator() -> None:
    assert _with_trailing_slash("https://office.example.com") == "https://office.example.com/"
    assert (
        _with_trailing_slash("http://wizard-stack-onlyoffice/") == "http://wizard-stack-onlyoffice/"
    )


def test_find_container_name_prefers_exact_compose_service_label_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_commands: list[list[str]] = []

    def fake_run(
        command: list[str], check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        recorded_commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                "openmerge-nextcloud-08k3b3-openmerge-nextcloud-1\n"
                "openmerge-nextcloud-08k3b3-openmerge-nextcloud-2\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("dokploy_wizard.dokploy.nextcloud.subprocess.run", fake_run)

    assert (
        _find_container_name("openmerge-nextcloud")
        == "openmerge-nextcloud-08k3b3-openmerge-nextcloud-1"
    )
    assert recorded_commands == [
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.service=openmerge-nextcloud",
            "--format",
            "{{.Names}}",
        ]
    ]


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
