# pyright: reportMissingImports=false

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from urllib import request

import pytest

from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
    DokploySeaweedFsBackend,
)
from dokploy_wizard.dokploy.seaweedfs import _docker_container_is_up, _local_https_health_check
from dokploy_wizard.packs.seaweedfs import (
    SEAWEEDFS_DATA_RESOURCE_TYPE,
    SEAWEEDFS_SERVICE_RESOURCE_TYPE,
    SeaweedFsResourceRecord,
    build_seaweedfs_ledger,
    reconcile_seaweedfs,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeSeaweedFsBackend:
    existing_service: SeaweedFsResourceRecord | None = None
    existing_data: SeaweedFsResourceRecord | None = None
    health_ok: bool = True
    update_service_calls: int = 0

    def get_service(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
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
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        del hostname, access_key, secret_key, data_resource_name
        self.existing_service = SeaweedFsResourceRecord(
            resource_id="seaweedfs-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

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
        self.update_service_calls += 1
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            access_key=access_key,
            secret_key=secret_key,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> SeaweedFsResourceRecord:
        self.existing_data = SeaweedFsResourceRecord(
            resource_id="seaweedfs-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: SeaweedFsResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


@dataclass
class FakeDokployApiClient:
    projects: list[DokployProjectSummary] = field(default_factory=list)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    deploy_calls: int = 0
    last_create_compose_file: str | None = None

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
        del compose_id, compose_file
        raise AssertionError("SeaweedFS backend should not update compose apps in this task")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def test_reconcile_seaweedfs_plans_runtime_when_enabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            },
        )
    )

    phase = reconcile_seaweedfs(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeSeaweedFsBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.hostname == "s3.example.com"
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-seaweedfs"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.resource_name == "wizard-stack-seaweedfs-data"


def test_reconcile_seaweedfs_reuses_owned_resources() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            },
        )
    )
    backend = FakeSeaweedFsBackend(
        existing_service=SeaweedFsResourceRecord(
            resource_id="seaweedfs-service-1",
            resource_name="wizard-stack-seaweedfs",
        ),
        existing_data=SeaweedFsResourceRecord(
            resource_id="seaweedfs-data-1",
            resource_name="wizard-stack-seaweedfs-data",
        ),
    )

    phase = reconcile_seaweedfs(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=SEAWEEDFS_SERVICE_RESOURCE_TYPE,
                    resource_id="seaweedfs-service-1",
                    scope="stack:wizard-stack:seaweedfs-service",
                ),
                OwnedResource(
                    resource_type=SEAWEEDFS_DATA_RESOURCE_TYPE,
                    resource_id="seaweedfs-data-1",
                    scope="stack:wizard-stack:seaweedfs-data",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None and phase.result.service.action == "update_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"
    assert backend.update_service_calls == 1


def test_reconcile_seaweedfs_reuses_existing_dokploy_managed_data() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            },
        )
    )
    backend = FakeSeaweedFsBackend(
        existing_data=SeaweedFsResourceRecord(
            resource_id="dokploy-compose:cmp-existing:seaweedfs-data",
            resource_name="wizard-stack-seaweedfs-data",
        )
    )

    phase = reconcile_seaweedfs(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_existing"


def test_reconcile_seaweedfs_reuses_existing_dokploy_managed_service() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SEAWEEDFS": "true",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            },
        )
    )
    backend = FakeSeaweedFsBackend(
        existing_service=SeaweedFsResourceRecord(
            resource_id="dokploy-compose:cmp-existing:seaweedfs-service",
            resource_name="wizard-stack-seaweedfs",
        )
    )

    phase = reconcile_seaweedfs(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_existing"
    assert backend.update_service_calls == 1


def test_dokploy_seaweedfs_backend_creates_one_compose_for_service_and_data() -> None:
    client = FakeDokployApiClient()
    backend = DokploySeaweedFsBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name="wizard-stack",
        hostname="s3.example.com",
        access_key="seaweed-access",
        secret_key="seaweed-secret",
        client=client,
    )

    data = backend.create_persistent_data("wizard-stack-seaweedfs-data")
    service = backend.create_service(
        resource_name="wizard-stack-seaweedfs",
        hostname="s3.example.com",
        access_key="seaweed-access",
        secret_key="seaweed-secret",
        data_resource_name="wizard-stack-seaweedfs-data",
    )

    assert data.resource_id == "dokploy-compose:cmp-1:seaweedfs-data"
    assert service.resource_id == "dokploy-compose:cmp-1:seaweedfs-service"
    assert client.create_project_calls == 1
    assert client.create_compose_calls == 1
    assert client.deploy_calls == 1
    compose = client.last_create_compose_file
    assert compose is not None
    assert "command: ['server', '-dir=/data', '-s3', '-ip.bind=0.0.0.0']" in compose
    assert 'traefik.http.routers.wizard-stack-seaweedfs.rule: "Host(`s3.example.com`)"' in compose
    assert (
        'traefik.http.services.wizard-stack-seaweedfs.loadbalancer.server.port: "8333"' in compose
    )


def test_build_seaweedfs_ledger_persists_service_and_data() -> None:
    updated = build_seaweedfs_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="seaweedfs-service-1",
        data_resource_id="seaweedfs-data-1",
    )

    assert {(resource.resource_type, resource.scope) for resource in updated.resources} == {
        (SEAWEEDFS_SERVICE_RESOURCE_TYPE, "stack:wizard-stack:seaweedfs-service"),
        (SEAWEEDFS_DATA_RESOURCE_TYPE, "stack:wizard-stack:seaweedfs-data"),
    }


def test_docker_container_is_up_matches_compose_named_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.seaweedfs.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "wizard-stack-seaweedfs-abc123-wizard-stack-seaweedfs-1\tUp 10 seconds\n",
            },
        )(),
    )

    assert _docker_container_is_up("wizard-stack-seaweedfs") is True


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

    monkeypatch.setattr("dokploy_wizard.dokploy.seaweedfs.request.urlopen", fake_urlopen)

    assert _local_https_health_check("https://s3.example.com/status") is True
    assert calls == [
        (
            "https://127.0.0.1/status",
            {"Host": "s3.example.com"},
            True,
        )
    ]
