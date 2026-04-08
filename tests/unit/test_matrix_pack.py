# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from dokploy_wizard.core.models import SharedCorePlan
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployMatrixBackend,
    DokployProjectSummary,
)
from dokploy_wizard.packs.matrix import (
    MATRIX_DATA_RESOURCE_TYPE,
    MATRIX_SERVICE_RESOURCE_TYPE,
    MatrixError,
    MatrixResourceRecord,
    build_matrix_ledger,
    reconcile_matrix,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeMatrixBackend:
    existing_service: MatrixResourceRecord | None = None
    existing_data: MatrixResourceRecord | None = None
    health_ok: bool = True
    create_service_calls: int = 0
    create_data_calls: int = 0

    def get_service(self, resource_id: str) -> MatrixResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
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
        shared_allocation: object,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> MatrixResourceRecord:
        del (
            hostname,
            secret_refs,
            shared_allocation,
            postgres_service_name,
            redis_service_name,
            data_resource_name,
        )
        self.create_service_calls += 1
        self.existing_service = MatrixResourceRecord(
            resource_id="matrix-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def get_persistent_data(self, resource_id: str) -> MatrixResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> MatrixResourceRecord:
        self.create_data_calls += 1
        self.existing_data = MatrixResourceRecord(
            resource_id="matrix-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool:
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
        raise AssertionError("Matrix backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_reconcile_matrix_plans_runtime_when_enabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )

    phase = reconcile_matrix(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeMatrixBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.enabled is True
    assert phase.result.hostname == "matrix.example.com"
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-matrix"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.resource_name == "wizard-stack-matrix-data"
    assert phase.result.shared_postgres_service == "wizard-stack-shared-postgres"
    assert phase.result.shared_redis_service == "wizard-stack-shared-redis"
    assert phase.result.shared_allocation is not None
    assert phase.result.shared_allocation.pack_name == "matrix"
    assert phase.result.shared_allocation.postgres is not None
    assert phase.result.shared_allocation.redis is not None
    assert phase.result.secret_refs == (
        "wizard-stack-matrix-registration-shared-secret",
        "wizard-stack-matrix-macaroon-secret-key",
    )
    assert phase.result.health_check is not None
    assert phase.result.health_check.url == "https://matrix.example.com/_matrix/client/versions"
    assert phase.result.health_check.passed is None


def test_reconcile_matrix_fails_closed_when_shared_core_dependency_missing() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )
    desired_state = replace(
        desired_state,
        shared_core=SharedCorePlan(
            network_name=desired_state.shared_core.network_name,
            postgres=desired_state.shared_core.postgres,
            redis=desired_state.shared_core.redis,
            allocations=(),
        ),
    )

    with pytest.raises(MatrixError, match="requires shared-core postgres, shared-core redis"):
        reconcile_matrix(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeMatrixBackend(),
        )


def test_reconcile_matrix_reuses_owned_resources_and_requires_health() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )
    backend = FakeMatrixBackend(
        existing_service=MatrixResourceRecord(
            resource_id="matrix-service-1",
            resource_name="wizard-stack-matrix",
        ),
        existing_data=MatrixResourceRecord(
            resource_id="matrix-data-1",
            resource_name="wizard-stack-matrix-data",
        ),
        health_ok=True,
    )

    phase = reconcile_matrix(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=MATRIX_SERVICE_RESOURCE_TYPE,
                    resource_id="matrix-service-1",
                    scope="stack:wizard-stack:matrix-service",
                ),
                OwnedResource(
                    resource_type=MATRIX_DATA_RESOURCE_TYPE,
                    resource_id="matrix-data-1",
                    scope="stack:wizard-stack:matrix-data",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.create_service_calls == 0
    assert backend.create_data_calls == 0


def test_reconcile_matrix_fails_closed_on_unowned_existing_service_collision() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )

    with pytest.raises(MatrixError, match="not wizard-owned"):
        reconcile_matrix(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeMatrixBackend(
                existing_service=MatrixResourceRecord(
                    resource_id="collision-service",
                    resource_name="wizard-stack-matrix",
                )
            ),
        )


def test_reconcile_matrix_fails_closed_on_health_check_failure() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )

    with pytest.raises(MatrixError, match="health check failed"):
        reconcile_matrix(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeMatrixBackend(health_ok=False),
        )


def test_build_matrix_ledger_persists_narrow_service_and_data_scopes() -> None:
    updated = build_matrix_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="matrix-service-1",
        data_resource_id="matrix-data-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=MATRIX_SERVICE_RESOURCE_TYPE,
            resource_id="matrix-service-1",
            scope="stack:wizard-stack:matrix-service",
        ),
        OwnedResource(
            resource_type=MATRIX_DATA_RESOURCE_TYPE,
            resource_id="matrix-data-1",
            scope="stack:wizard-stack:matrix-data",
        ),
    )


def test_dokploy_matrix_backend_creates_one_compose_for_service_and_data() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MATRIX": "true",
            },
        )
    )
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "matrix"
    )
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    client = FakeDokployApiClient()
    backend = DokployMatrixBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name=desired_state.stack_name,
        hostname=desired_state.hostnames["matrix"],
        shared_allocation=allocation,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        secret_refs=(
            "wizard-stack-matrix-registration-shared-secret",
            "wizard-stack-matrix-macaroon-secret-key",
        ),
        client=client,
    )

    data = backend.create_persistent_data("wizard-stack-matrix-data")
    service = backend.create_service(
        resource_name="wizard-stack-matrix",
        hostname="matrix.example.com",
        secret_refs=(
            "wizard-stack-matrix-registration-shared-secret",
            "wizard-stack-matrix-macaroon-secret-key",
        ),
        shared_allocation=allocation,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        data_resource_name="wizard-stack-matrix-data",
    )

    assert data.resource_id == "dokploy-compose:cmp-1:matrix-data"
    assert service.resource_id == "dokploy-compose:cmp-1:matrix-service"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
