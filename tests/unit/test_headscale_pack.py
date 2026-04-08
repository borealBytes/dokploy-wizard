# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployHeadscaleBackend,
    DokployProjectSummary,
)
from dokploy_wizard.packs.headscale import (
    HEADSCALE_SERVICE_RESOURCE_TYPE,
    HeadscaleError,
    HeadscaleResourceRecord,
    build_headscale_ledger,
    reconcile_headscale,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None
    health_ok: bool = True
    create_calls: int = 0

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
        self.create_calls += 1
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


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
        raise AssertionError("Headscale backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_reconcile_headscale_plans_runtime_when_enabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
        )
    )

    phase = reconcile_headscale(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeHeadscaleBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.enabled is True
    assert phase.result.hostname == "headscale.example.com"
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-headscale"
    assert phase.result.secret_refs == (
        "wizard-stack-headscale-admin-api-key",
        "wizard-stack-headscale-noise-private-key",
    )
    assert phase.result.health_check is not None
    assert phase.result.health_check.url == "https://headscale.example.com/health"
    assert phase.result.health_check.passed is None


def test_reconcile_headscale_skips_cleanly_when_disabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "false",
            },
        )
    )

    phase = reconcile_headscale(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="cloudflare_tunnel",
                    resource_id="tunnel-1",
                    scope="account:account-123",
                ),
            ),
        ),
        backend=FakeHeadscaleBackend(),
    )

    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False
    assert phase.service_resource_id is None

    updated_ledger = build_headscale_ledger(
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
        service_resource_id=phase.service_resource_id,
    )
    assert updated_ledger.resources == (
        OwnedResource(
            resource_type="cloudflare_tunnel",
            resource_id="tunnel-1",
            scope="account:account-123",
        ),
    )


def test_reconcile_headscale_reuses_owned_service_and_requires_health() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
        )
    )
    backend = FakeHeadscaleBackend(
        existing_service=HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name="wizard-stack-headscale",
        ),
        health_ok=True,
    )

    phase = reconcile_headscale(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=HEADSCALE_SERVICE_RESOURCE_TYPE,
                    resource_id="headscale-service-1",
                    scope="stack:wizard-stack:headscale",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_owned"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.create_calls == 0


def test_reconcile_headscale_fails_closed_on_health_check_failure() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
        )
    )

    with pytest.raises(HeadscaleError, match="health check failed"):
        reconcile_headscale(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeHeadscaleBackend(health_ok=False),
        )


def test_build_headscale_ledger_persists_narrow_service_scope() -> None:
    updated = build_headscale_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="headscale-service-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=HEADSCALE_SERVICE_RESOURCE_TYPE,
            resource_id="headscale-service-1",
            scope="stack:wizard-stack:headscale",
        ),
    )


def test_dokploy_headscale_backend_creates_and_reuses_compose_service() -> None:
    client = FakeDokployApiClient()
    backend = DokployHeadscaleBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name="wizard-stack",
        hostname="headscale.example.com",
        client=client,
    )

    created = backend.create_service(
        resource_name="wizard-stack-headscale",
        hostname="headscale.example.com",
        secret_refs=(
            "wizard-stack-headscale-admin-api-key",
            "wizard-stack-headscale-noise-private-key",
        ),
    )
    reused = backend.get_service(created.resource_id)

    assert created.resource_name == "wizard-stack-headscale"
    assert created.resource_id == "dokploy-compose:cmp-1:headscale"
    assert reused is not None
    assert reused.resource_id == created.resource_id
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
