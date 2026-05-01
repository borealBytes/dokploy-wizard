# pyright: reportMissingImports=false

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import dokploy_wizard.cli
from dokploy_wizard.cli import run_install_flow, run_modify_flow
from dokploy_wizard.core import SharedCoreResourceRecord
from dokploy_wizard.core.models import SharedPostgresAllocation
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
from dokploy_wizard.dokploy import nextcloud as nextcloud_module
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.packs.nextcloud import NextcloudError, NextcloudResourceRecord
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudBundleVerification,
    NextcloudCommandCheck,
    TalkRuntime,
)
from dokploy_wizard.packs.openclaw import OpenClawResourceRecord
from dokploy_wizard.packs.seaweedfs import SeaweedFsResourceRecord
from dokploy_wizard.state import RawEnvInput, load_state_dir, resolve_desired_state
from tests.integration.test_networking_reconciler import FakeCoderBackend

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


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
        del account_id

    def validate_zone_access(self, zone_id: str) -> None:
        del zone_id

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        del account_id
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        del account_id
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        del account_id
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
        del zone_id, record_type
        record = self.dns_records.get(hostname)
        if record is None:
            return ()
        if content is not None and record.content != content:
            return ()
        return (record,)

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        del zone_id
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

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_id == resource_id:
            return self.network
        return None

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        if self.network is not None and self.network.resource_name == resource_name:
            return self.network
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        self.network = SharedCoreResourceRecord(
            resource_id="network-1", resource_name=resource_name
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
        self.postgres = SharedCoreResourceRecord(
            resource_id="postgres-1", resource_name=resource_name
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
        self.redis = SharedCoreResourceRecord(resource_id="redis-1", resource_name=resource_name)
        return self.redis

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        del resource_id
        return None

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        del resource_name
        return None

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        raise AssertionError(f"Nextcloud should not provision mail relay: {resource_name}")

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        del allocations


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
    health: dict[str, bool] = field(default_factory=dict)
    create_service_calls: int = 0
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
class RecordingNextcloudBackend(FakeNextcloudBackend):
    init_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeSeaweedFsBackend:
    def get_service(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        del resource_id
        return None

    def find_service_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        del resource_name
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        del hostname, access_key, secret_key, data_resource_name
        return SeaweedFsResourceRecord(
            resource_id=f"service:{resource_name}", resource_name=resource_name
        )

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            access_key=access_key,
            secret_key=secret_key,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        del resource_id
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        del resource_name
        return None

    def create_persistent_data(self, resource_name: str) -> SeaweedFsResourceRecord:
        return SeaweedFsResourceRecord(
            resource_id=f"volume:{resource_name}", resource_name=resource_name
        )

    def check_health(self, *, service: SeaweedFsResourceRecord, url: str) -> bool:
        del service, url
        return True


@dataclass
class FakeOpenClawBackend:
    services: dict[str, OpenClawResourceRecord] = field(default_factory=dict)

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        for record in self.services.values():
            if record.resource_id == resource_id:
                return record
        return None

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
        return self.services.get(resource_name)

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, replicas, secret_refs
        record = OpenClawResourceRecord(
            resource_id=f"service:{resource_name}",
            resource_name=resource_name,
            replicas=1,
        )
        self.services[resource_name] = record
        return record

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            template_path=template_path,
            variant=variant,
            channels=channels,
            replicas=replicas,
            secret_refs=secret_refs,
        )

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service, url
        return True


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    deploy_calls: int = 0

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
        del compose_file, app_name
        self.create_compose_calls += 1
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
        del compose_id, compose_file
        raise AssertionError("Nextcloud backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        del compose_id
        return ()

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
        return DokployScheduleRecord(
            schedule_id="sch-1",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )

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
        return DokployScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )

    def delete_schedule(self, *, schedule_id: str) -> None:
        del schedule_id


def _owned_dns_records() -> dict[str, CloudflareDnsRecord]:
    return {
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
    }


def _base_install_values(**overrides: str) -> dict[str, str]:
    values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "HOST_OS_ID": "ubuntu",
        "HOST_OS_VERSION_ID": "24.04",
        "HOST_CPU_COUNT": "4",
        "HOST_MEMORY_GB": "8",
        "HOST_DISK_GB": "100",
        "HOST_DOCKER_INSTALLED": "true",
        "HOST_DOCKER_DAEMON_REACHABLE": "true",
        "HOST_PORT_80_IN_USE": "false",
        "HOST_PORT_443_IN_USE": "false",
        "HOST_PORT_3000_IN_USE": "false",
        "HOST_ENVIRONMENT": "local",
        "DOKPLOY_BOOTSTRAP_HEALTHY": "true",
        "DOKPLOY_API_URL": "https://dokploy.example.com/api",
        "DOKPLOY_API_KEY": "api-key-123",
        "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
        "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        "CLOUDFLARE_API_TOKEN": "token-123",
        "CLOUDFLARE_ACCOUNT_ID": "account-123",
        "CLOUDFLARE_ZONE_ID": "zone-123",
        "CLOUDFLARE_TUNNEL_NAME": "wizard-stack-tunnel",
    }
    values.update(overrides)
    return values


def _passing_bundle_verification() -> NextcloudBundleVerification:
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


def _openclaw_nexa_values() -> dict[str, str]:
    return {
        "ENABLE_OPENCLAW": "true",
        "OPENCLAW_CHANNELS": "telegram",
        "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0.internal.example.com",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
        "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
        "OPENCLAW_NEXA_AGENT_PASSWORD": "nexa-secret",
        "OPENCLAW_NEXA_AGENT_EMAIL": "nexa@example.com",
    }


def _farm_values() -> dict[str, str]:
    return {
        "ENABLE_MY_FARM_ADVISOR": "true",
        "MY_FARM_ADVISOR_CHANNELS": "telegram",
        "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
    }


@dataclass
class RecordingNextcloudApi(FakeDokployApiClient):
    compose_files_by_id: dict[str, str] = field(default_factory=dict)
    compose_names_by_id: dict[str, str] = field(default_factory=dict)
    schedules: list[DokployScheduleRecord] = field(default_factory=list)
    update_compose_calls: int = 0

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        record = super().create_compose(
            name=name,
            environment_id=environment_id,
            compose_file=compose_file,
            app_name=app_name,
        )
        self.compose_files_by_id[record.compose_id] = compose_file
        self.compose_names_by_id[record.compose_id] = name
        return record

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.update_compose_calls += 1
        self.compose_files_by_id[compose_id] = compose_file
        return DokployComposeRecord(
            compose_id=compose_id,
            name=self.compose_names_by_id.get(compose_id, f"compose-{compose_id}"),
        )

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
        del compose_id
        record = DokployScheduleRecord(
            schedule_id=f"schedule-{len(self.schedules) + 1}",
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
        del compose_id
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
        for index, existing in enumerate(self.schedules):
            if existing.schedule_id == schedule_id:
                self.schedules[index] = record
                break
        else:
            self.schedules.append(record)
        return record


@dataclass
class NextcloudOccRecorder:
    mounts: list[dict[str, object]] = field(default_factory=list)
    commands: list[tuple[str, ...]] = field(default_factory=list)
    shell_commands: list[str] = field(default_factory=list)
    next_mount_id: int = 1

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nextcloud_module, "_nextcloud_status_ready", lambda url: True)
        monkeypatch.setattr(nextcloud_module, "_local_https_health_check", lambda url: True)
        monkeypatch.setattr(
            nextcloud_module,
            "_find_container_name",
            lambda service_name: "nextcloud-container",
        )
        monkeypatch.setattr(nextcloud_module, "_ensure_admin_user", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            nextcloud_module,
            "_ensure_nexa_service_account",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(nextcloud_module, "_nextcloud_user_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(nextcloud_module, "_ensure_trusted_domain", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            nextcloud_module,
            "_ensure_external_storage_path",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(nextcloud_module, "_list_external_storage_mounts", lambda _: tuple(self.mounts))
        monkeypatch.setattr(nextcloud_module, "_run_occ", self._run_occ)
        monkeypatch.setattr(nextcloud_module, "_run_occ_shell", self._run_occ_shell)
        monkeypatch.setattr(
            nextcloud_module,
            "_verify_nextcloud_bundle",
            lambda container_name: _passing_bundle_verification(),
        )

    def _run_occ_shell(self, container_name: str, shell_command: str) -> None:
        assert container_name == "nextcloud-container"
        self.shell_commands.append(shell_command)

    def _run_occ(self, container_name: str, args: list[str]) -> None:
        assert container_name == "nextcloud-container"
        command = tuple(args)
        self.commands.append(command)
        if len(args) >= 6 and args[0] == "files_external:create":
            self.mounts.append(
                {
                    "mount_id": self.next_mount_id,
                    "mount_point": args[1],
                    "configuration": {"datadir": args[5].split("=", 1)[1]},
                }
            )
            self.next_mount_id += 1
            return
        if args[:2] == ["files_external:delete", "--yes"]:
            self.mounts[:] = [item for item in self.mounts if str(item["mount_id"]) != args[2]]

    def mount_pairs(self) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for item in self.mounts:
            configuration = item["configuration"]
            assert isinstance(configuration, dict)
            pairs.add((str(item["mount_point"]), str(configuration["datadir"])))
        return pairs

    def mount_id(self, mount_point: str) -> str | None:
        for item in self.mounts:
            if item["mount_point"] == mount_point:
                return str(item["mount_id"])
        return None


def _files_external_create_commands(
    commands: list[tuple[str, ...]],
) -> list[tuple[str, ...]]:
    return [command for command in commands if command[:1] == ("files_external:create",)]


def _files_scan_commands(commands: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [command for command in commands if command[:1] == ("files:scan",)]


def _patch_real_dokploy_nextcloud_backend(
    monkeypatch: pytest.MonkeyPatch, api: RecordingNextcloudApi
) -> None:
    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)

    def _build_backend(**kwargs: Any) -> DokployNextcloudBackend:
        kwargs["client"] = api
        return DokployNextcloudBackend(**kwargs)

    monkeypatch.setattr(dokploy_wizard.cli, "DokployNextcloudBackend", _build_backend)


def test_install_farm_only_nextcloud_creates_both_farm_external_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = RecordingNextcloudApi()
    occ = NextcloudOccRecorder()
    _patch_real_dokploy_nextcloud_backend(monkeypatch, api)
    occ.patch(monkeypatch)

    summary = run_install_flow(
        env_file=tmp_path / "farm-only.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values=_base_install_values(
                ENABLE_NEXTCLOUD="true",
                AI_DEFAULT_API_KEY="shared-ai-key",
                AI_DEFAULT_BASE_URL="https://models.example.com/v1",
                CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
                **_farm_values(),
            ),
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=FakeOpenClawBackend(),
    )

    assert summary["nextcloud"]["outcome"] == "applied"
    assert occ.mount_pairs() == {
        ("/Nexa Farm", "/mnt/advisors/my-farm-advisor/field-operations"),
        ("/Nexa Farm Data Pipeline", "/mnt/advisors/my-farm-advisor/data-pipeline"),
    }
    assert _files_external_create_commands(occ.commands) == [
        (
            "files_external:create",
            "/Nexa Farm",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/field-operations",
        ),
        (
            "files_external:create",
            "/Nexa Farm Data Pipeline",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/data-pipeline",
        ),
    ]
    assert (
        "files:scan",
        "--path=admin@example.com/files/Nexa Farm",
    ) in _files_scan_commands(occ.commands)
    assert (
        "files:scan",
        "--path=admin@example.com/files/Nexa Farm Data Pipeline",
    ) in _files_scan_commands(occ.commands)


def test_install_both_advisors_nextcloud_creates_openclaw_and_farm_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = RecordingNextcloudApi()
    occ = NextcloudOccRecorder()
    _patch_real_dokploy_nextcloud_backend(monkeypatch, api)
    occ.patch(monkeypatch)

    summary = run_install_flow(
        env_file=tmp_path / "both-advisors.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values=_base_install_values(
                ENABLE_NEXTCLOUD="true",
                AI_DEFAULT_API_KEY="shared-ai-key",
                AI_DEFAULT_BASE_URL="https://models.example.com/v1",
                CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
                **_openclaw_nexa_values(),
                **_farm_values(),
            ),
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=FakeOpenClawBackend(),
    )

    assert summary["nextcloud"]["outcome"] == "applied"
    assert occ.mount_pairs() == {
        ("/Nexa Claw", "/mnt/advisors/openclaw/workspace"),
        ("/Nexa Farm", "/mnt/advisors/my-farm-advisor/field-operations"),
        ("/Nexa Farm Data Pipeline", "/mnt/advisors/my-farm-advisor/data-pipeline"),
    }
    assert _files_external_create_commands(occ.commands) == [
        (
            "files_external:create",
            "/Nexa Claw",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/openclaw/workspace",
        ),
        (
            "files_external:create",
            "/Nexa Farm",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/field-operations",
        ),
        (
            "files_external:create",
            "/Nexa Farm Data Pipeline",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/data-pipeline",
        ),
    ]
    assert (
        "files:scan",
        "--path=admin@example.com/files/Nexa Claw",
    ) in _files_scan_commands(occ.commands)


def test_install_openclaw_only_nextcloud_preserves_legacy_openclaw_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = RecordingNextcloudApi()
    occ = NextcloudOccRecorder()
    _patch_real_dokploy_nextcloud_backend(monkeypatch, api)
    occ.patch(monkeypatch)
    occ.mounts.append(
        {
            "mount_id": 17,
            "mount_point": "/OpenClaw",
            "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
        }
    )
    occ.next_mount_id = 18

    summary = run_install_flow(
        env_file=tmp_path / "openclaw-only.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values=_base_install_values(
                ENABLE_NEXTCLOUD="true",
                AI_DEFAULT_API_KEY="shared-ai-key",
                AI_DEFAULT_BASE_URL="https://models.example.com/v1",
                CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
                **_openclaw_nexa_values(),
            ),
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=FakeOpenClawBackend(),
    )

    assert summary["nextcloud"]["outcome"] == "applied"
    assert occ.mount_pairs() == {("/OpenClaw", "/mnt/advisors/openclaw/workspace")}
    assert _files_external_create_commands(occ.commands) == []
    assert not any(command[1] == "/Nexa Claw" for command in _files_external_create_commands(occ.commands))
    assert (
        "files:scan",
        "--path=admin@example.com/files/OpenClaw",
    ) in _files_scan_commands(occ.commands)


def test_modify_adds_farm_mounts_to_existing_nextcloud_without_recreating_openclaw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "modify-add-farm.env"
    api = RecordingNextcloudApi()
    occ = NextcloudOccRecorder()
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    openclaw_backend = FakeOpenClawBackend()
    _patch_real_dokploy_nextcloud_backend(monkeypatch, api)
    occ.patch(monkeypatch)
    occ.mounts.append(
        {
            "mount_id": 17,
            "mount_point": "/OpenClaw",
            "configuration": {"datadir": "/mnt/advisors/openclaw/workspace"},
        }
    )
    occ.next_mount_id = 18

    initial_raw = RawEnvInput(
        format_version=1,
        values=_base_install_values(
            ENABLE_NEXTCLOUD="true",
            AI_DEFAULT_API_KEY="shared-ai-key",
            AI_DEFAULT_BASE_URL="https://models.example.com/v1",
            CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
            **_openclaw_nexa_values(),
        ),
    )
    modified_raw = RawEnvInput(
        format_version=1,
        values=_base_install_values(
            ENABLE_NEXTCLOUD="true",
            AI_DEFAULT_API_KEY="shared-ai-key",
            AI_DEFAULT_BASE_URL="https://models.example.com/v1",
            CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
            **_openclaw_nexa_values(),
            **_farm_values(),
        ),
    )

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=initial_raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=openclaw_backend,
    )

    openclaw_mount_id = occ.mount_id("/OpenClaw")
    command_count_before_modify = len(occ.commands)
    summary = run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=modified_raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=openclaw_backend,
    )
    modify_commands = occ.commands[command_count_before_modify:]

    assert {"nextcloud", "my-farm-advisor", "cloudflare_access"}.issubset(
        set(summary["lifecycle"]["phases_to_run"])
    )
    assert occ.mount_id("/OpenClaw") == openclaw_mount_id
    assert occ.mount_pairs() == {
        ("/OpenClaw", "/mnt/advisors/openclaw/workspace"),
        ("/Nexa Farm", "/mnt/advisors/my-farm-advisor/field-operations"),
        ("/Nexa Farm Data Pipeline", "/mnt/advisors/my-farm-advisor/data-pipeline"),
    }
    assert _files_external_create_commands(modify_commands) == [
        (
            "files_external:create",
            "/Nexa Farm",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/field-operations",
        ),
        (
            "files_external:create",
            "/Nexa Farm Data Pipeline",
            "local",
            "null::null",
            "-c",
            "datadir=/mnt/advisors/my-farm-advisor/data-pipeline",
        ),
    ]
    assert not any(command[1] in {"/OpenClaw", "/Nexa Claw"} for command in _files_external_create_commands(modify_commands))
    assert not any(command[:2] == ("files_external:delete", "--yes") for command in modify_commands)
    assert (
        "files:scan",
        "--path=admin@example.com/files/OpenClaw",
    ) in _files_scan_commands(modify_commands)


def test_rerun_with_both_advisors_is_idempotent_for_nextcloud_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "rerun-both-advisors.env"
    api = RecordingNextcloudApi()
    occ = NextcloudOccRecorder()
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    openclaw_backend = FakeOpenClawBackend()
    _patch_real_dokploy_nextcloud_backend(monkeypatch, api)
    occ.patch(monkeypatch)

    raw_env = RawEnvInput(
        format_version=1,
        values=_base_install_values(
            ENABLE_NEXTCLOUD="true",
            AI_DEFAULT_API_KEY="shared-ai-key",
            AI_DEFAULT_BASE_URL="https://models.example.com/v1",
            CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
            **_openclaw_nexa_values(),
            **_farm_values(),
        ),
    )

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=openclaw_backend,
    )

    command_count_before_rerun = len(occ.commands)
    mount_pairs_before_rerun = occ.mount_pairs().copy()
    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=None,
        openclaw_backend=openclaw_backend,
    )

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["nextcloud"]["outcome"] == "already_present"
    assert len(occ.commands) == command_count_before_rerun
    assert occ.mount_pairs() == mount_pairs_before_rerun


def test_install_reconciles_nextcloud_pair_and_persists_runtime_ledger(tmp_path: Path) -> None:
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

    assert summary["nextcloud"]["outcome"] == "applied"
    assert (
        summary["nextcloud"]["nextcloud"]["service"]["resource_name"] == "nextcloud-stack-nextcloud"
    )
    assert (
        summary["nextcloud"]["onlyoffice"]["service"]["resource_name"]
        == "nextcloud-stack-onlyoffice"
    )
    assert (
        summary["nextcloud"]["nextcloud"]["config"]["onlyoffice_url"]
        == "https://office.example.com"
    )
    assert (
        summary["nextcloud"]["onlyoffice"]["config"]["integration_secret_ref"]
        == "nextcloud-stack-nextcloud-onlyoffice-jwt-secret"
    )
    assert summary["nextcloud"]["nextcloud"]["health_check"]["passed"] is True
    assert summary["nextcloud"]["onlyoffice"]["health_check"]["passed"] is True
    assert summary["nextcloud"]["onlyoffice"]["document_server_check"]["passed"] is True
    assert summary["nextcloud"]["talk"]["app_id"] == "spreed"
    assert summary["nextcloud"]["talk"]["enabled"] is True
    assert summary["nextcloud"]["talk"]["enabled_check"]["passed"] is True
    assert summary["nextcloud"]["talk"]["signaling_check"]["passed"] is True
    assert summary["nextcloud"]["talk"]["stun_check"]["passed"] is True
    assert summary["nextcloud"]["talk"]["turn_check"]["passed"] is True
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
        ("shared_core_network", "stack:nextcloud-stack:shared-network"),
        ("shared_core_postgres", "stack:nextcloud-stack:shared-postgres"),
        ("shared_core_redis", "stack:nextcloud-stack:shared-redis"),
        ("nextcloud_service", "stack:nextcloud-stack:nextcloud-service"),
        ("onlyoffice_service", "stack:nextcloud-stack:onlyoffice-service"),
        ("nextcloud_volume", "stack:nextcloud-stack:nextcloud-volume"),
        ("onlyoffice_volume", "stack:nextcloud-stack:onlyoffice-volume"),
    }


def test_install_reconciles_nextcloud_pair_via_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
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
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._nextcloud_status_ready",
        lambda url: url == "https://nextcloud.example.com/status.php",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._local_https_health_check",
        lambda url: url == "https://office.example.com/healthcheck",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._find_container_name",
        lambda service_name: "nextcloud-container",
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_admin_user",
        lambda container_name, admin_user, admin_password: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_trusted_domain",
        lambda container_name, hostname: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._ensure_onlyoffice_app_config",
        lambda container_name,
        document_server_url,
        document_server_internal_url,
        storage_url,
        jwt_secret,
        wait_for_documentserver_check=False,
        advisor_workspace_mounts=(),
        openclaw_external_storage_enabled=False,
        openclaw_external_storage_mount_point="/Nexa Claw",
        openclaw_external_storage_datadir="/mnt/advisors/openclaw/workspace",
        openclaw_external_storage_volume_root="/mnt/advisors/openclaw",
        admin_user="admin": None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.nextcloud._verify_nextcloud_bundle",
        lambda container_name: NextcloudBundleVerification(
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
        ),
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=DokployNextcloudBackend(
            api_url="https://dokploy.example.com",
            api_key="dokp-key-123",
            stack_name=desired_state.stack_name,
            nextcloud_hostname=desired_state.hostnames["nextcloud"],
            onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
            postgres_service_name=desired_state.shared_core.postgres.service_name,
            redis_service_name=desired_state.shared_core.redis.service_name,
            postgres=allocation.postgres,
            redis=allocation.redis,
            integration_secret_ref="nextcloud-stack-nextcloud-onlyoffice-jwt-secret",
            client=client,
        ),
    )

    loaded_state = load_state_dir(state_dir)
    assert summary["nextcloud"]["outcome"] == "applied"
    assert summary["nextcloud"]["nextcloud"]["service"]["resource_id"] == (
        "dokploy-compose:cmp-1:nextcloud-service"
    )
    assert summary["nextcloud"]["onlyoffice"]["service"]["resource_id"] == (
        "dokploy-compose:cmp-1:onlyoffice-service"
    )
    assert summary["nextcloud"]["talk"]["enabled"] is True
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    assert loaded_state.ownership_ledger is not None


def test_install_passes_nexa_workspace_contract_into_dokploy_nextcloud_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "nextcloud-nexa.env"
    recording_backend = RecordingNextcloudBackend()

    def _build_backend(**kwargs: Any) -> RecordingNextcloudBackend:
        recording_backend.init_kwargs = dict(kwargs)
        return recording_backend

    monkeypatch.setattr("dokploy_wizard.cli.DokployNextcloudBackend", _build_backend)
    monkeypatch.setattr("dokploy_wizard.cli._can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr("dokploy_wizard.cli._qualify_dokploy_mutation_auth", lambda **_: None)

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "nextcloud-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_NEXTCLOUD": "true",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0.internal.example.com",
                "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
                "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
                "OPENCLAW_NEXA_AGENT_PASSWORD": "nexa-secret",
                "OPENCLAW_NEXA_AGENT_EMAIL": "nexa@example.com",
                "HOST_OS_ID": "ubuntu",
                "HOST_OS_VERSION_ID": "24.04",
                "HOST_CPU_COUNT": "4",
                "HOST_MEMORY_GB": "8",
                "HOST_DISK_GB": "100",
                "HOST_DOCKER_INSTALLED": "true",
                "HOST_DOCKER_DAEMON_REACHABLE": "true",
                "HOST_PORT_80_IN_USE": "false",
                "HOST_PORT_443_IN_USE": "false",
                "HOST_PORT_3000_IN_USE": "false",
                "HOST_ENVIRONMENT": "local",
                "DOKPLOY_BOOTSTRAP_HEALTHY": "true",
                "DOKPLOY_API_URL": "https://dokploy.example.com/api",
                "DOKPLOY_API_KEY": "api-key-123",
                "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
                "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "CLOUDFLARE_TUNNEL_NAME": "nextcloud-stack-tunnel",
                "HEADSCALE_TAILNET_DOMAIN": "tailnet.example.com",
                "HEADSCALE_ACME_EMAIL": "admin@example.com",
                "HEADSCALE_OIDC_ISSUER_URL": "https://auth.example.com/application/o/headscale/",
                "HEADSCALE_OIDC_CLIENT_ID": "headscale-client",
                "HEADSCALE_OIDC_CLIENT_SECRET": "headscale-secret",
                "HEADSCALE_OIDC_STRIP_EMAIL_DOMAIN": "true",
            },
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        openclaw_backend=FakeOpenClawBackend(),
        nextcloud_backend=None,
    )

    assert summary["nextcloud"]["outcome"] == "applied"
    mounts = recording_backend.init_kwargs["advisor_workspace_mounts"]
    assert len(mounts) == 1
    assert mounts[0].advisor_id == "openclaw"
    assert mounts[0].external_mount_name == "/OpenClaw"
    assert mounts[0].external_mount_path == "/mnt/openclaw/workspace"
    assert mounts[0].visible_root == "/mnt/openclaw/workspace/nexa"
    assert mounts[0].contract_path == "/mnt/openclaw/workspace/nexa/contract.json"
    assert mounts[0].runtime_state_source == "server-owned env + durable state JSON"
    assert recording_backend.init_kwargs["nexa_agent_user_id"] == "nexa-agent"
    assert recording_backend.init_kwargs["nexa_agent_display_name"] == "Nexa"
    assert recording_backend.init_kwargs["nexa_agent_password"] == "nexa-secret"
    assert recording_backend.init_kwargs["nexa_agent_email"] == "nexa@example.com"


def test_install_rerun_reuses_owned_nextcloud_resources(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    nextcloud_backend = FakeNextcloudBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=nextcloud_backend,
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(
            existing_tunnel=CloudflareTunnel(
                tunnel_id="nextcloud-tunnel", name="nextcloud-stack-tunnel"
            ),
            dns_records=_owned_dns_records(),
        ),
        shared_core_backend=FakeSharedCoreBackend(
            network=SharedCoreResourceRecord(
                resource_id="network-1", resource_name="nextcloud-stack-shared"
            ),
            postgres=SharedCoreResourceRecord(
                resource_id="postgres-1", resource_name="nextcloud-stack-shared-postgres"
            ),
            redis=SharedCoreResourceRecord(
                resource_id="redis-1", resource_name="nextcloud-stack-shared-redis"
            ),
        ),
        headscale_backend=FakeHeadscaleBackend(
            existing_service=HeadscaleResourceRecord(
                resource_id="headscale-service-1",
                resource_name="nextcloud-stack-headscale",
            )
        ),
        nextcloud_backend=nextcloud_backend,
    )

    assert summary["nextcloud"]["outcome"] == "already_present"
    assert summary["nextcloud"]["nextcloud"]["service"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["onlyoffice"]["service"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["nextcloud"]["data_volume"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["onlyoffice"]["data_volume"]["action"] == "reuse_owned"
    assert summary["nextcloud"]["talk"]["app_id"] == "spreed"
    assert nextcloud_backend.create_service_calls == 2
    assert nextcloud_backend.create_volume_calls == 2


def test_install_fails_before_nextcloud_checkpoint_when_onlyoffice_health_fails(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"

    with pytest.raises(NextcloudError, match="OnlyOffice health check failed"):
        run_install_flow(
            env_file=FIXTURES_DIR / "nextcloud.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(),
            nextcloud_backend=FakeNextcloudBackend(health={"nextcloud-stack-onlyoffice": False}),
        )

    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    assert "nextcloud" not in loaded_state.applied_state.completed_steps


def test_install_fails_before_nextcloud_checkpoint_when_talk_verification_fails(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"

    @dataclass
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
        run_install_flow(
            env_file=FIXTURES_DIR / "nextcloud.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
            shared_core_backend=FakeSharedCoreBackend(),
            headscale_backend=FakeHeadscaleBackend(),
            nextcloud_backend=TalkDisabledBackend(),
        )

    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    assert "nextcloud" not in loaded_state.applied_state.completed_steps


def test_openclaw_phase_refreshes_nextcloud_external_storage(tmp_path: Path) -> None:
    nextcloud_backend = FakeNextcloudBackend()

    run_install_flow(
        env_file=FIXTURES_DIR / "full.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=nextcloud_backend,
        seaweedfs_backend=FakeSeaweedFsBackend(),
        coder_backend=FakeCoderBackend(),
        openclaw_backend=FakeOpenClawBackend(),
    )

    assert nextcloud_backend.refresh_calls == ["admin"]
