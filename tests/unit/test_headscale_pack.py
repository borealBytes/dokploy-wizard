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
from dokploy_wizard.dokploy.headscale import _render_compose_file
from dokploy_wizard.packs.headscale import (
    HEADSCALE_SERVICE_RESOURCE_TYPE,
    HeadscaleError,
    HeadscaleResourceRecord,
    build_headscale_ledger,
    reconcile_headscale,
)
from dokploy_wizard.packs.headscale.reconciler import _http_health_check
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
        del compose_file
        return DokployComposeRecord(compose_id=compose_id, name="wizard-stack-headscale")

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
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "true",
            },
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
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "true",
            },
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


def test_reconcile_headscale_adopts_matching_existing_service_by_name() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "true",
            },
        )
    )
    backend = FakeHeadscaleBackend(
        existing_service=HeadscaleResourceRecord(
            resource_id="headscale-service-existing",
            resource_name="wizard-stack-headscale",
        ),
        health_ok=True,
    )

    phase = reconcile_headscale(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_existing"
    assert phase.service_resource_id == "headscale-service-1"
    assert backend.create_calls == 1


def test_reconcile_headscale_fails_closed_on_health_check_failure() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_HEADSCALE": "true",
            },
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


def test_dokploy_headscale_backend_redeploys_existing_compose_service() -> None:
    client = FakeDokployApiClient(
        projects=[
            DokployProjectSummary(
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
                                name="wizard-stack-headscale",
                                status=None,
                            ),
                        ),
                    ),
                ),
            )
        ]
    )
    backend = DokployHeadscaleBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name="wizard-stack",
        hostname="headscale.example.com",
        client=client,
    )

    reused = backend.create_service(
        resource_name="wizard-stack-headscale",
        hostname="headscale.example.com",
        secret_refs=(
            "wizard-stack-headscale-admin-api-key",
            "wizard-stack-headscale-noise-private-key",
        ),
    )

    assert reused.resource_id == "dokploy-compose:cmp-existing:headscale"
    assert client.create_project_calls == 0
    assert client.create_compose_calls == 0
    assert client.deploy_calls == 1


def test_dokploy_headscale_compose_renders_without_heredoc() -> None:
    rendered = _render_compose_file(
        "wizard-stack-headscale",
        "headscale.example.com",
        (
            "wizard-stack-headscale-admin-api-key",
            "wizard-stack-headscale-noise-private-key",
        ),
    )

    assert "cat <<'EOF'" not in rendered
    assert "HEADSCALE_SERVER_URL: https://headscale.example.com" in rendered
    assert "HEADSCALE_LOG_LEVEL: info" in rendered
    assert "HEADSCALE_DNS_MAGIC_DNS: 'false'" in rendered
    assert "HEADSCALE_DERP_URLS: https://controlplane.tailscale.com/derpmap/default" in rendered
    assert "command: ['serve']" in rendered
    assert "HEADSCALE_DERP_SERVER_ENABLED" not in rendered
    assert "healthcheck:" not in rendered
    assert (
        'traefik.http.routers.wizard-stack-headscale.rule: "Host(`headscale.example.com`)"'
        in rendered
    )
    assert (
        'traefik.http.services.wizard-stack-headscale.loadbalancer.server.port: "8080"' in rendered
    )
    assert "      - dokploy-network" in rendered


def test_headscale_health_check_falls_back_to_loopback_with_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale._docker_container_is_up",
        lambda service_name: False,
    )
    calls: list[tuple[str, dict[str, str], bool]] = []

    class FakeConnection:
        def __init__(self, host: str, *, timeout: float, context: object | None = None) -> None:
            del timeout
            calls.append((host, {}, context is not None))
            self._host = host

        def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
            del method, path
            calls[-1] = (self._host, headers or {}, calls[-1][2])
            if self._host != "127.0.0.1":
                raise OSError("public hostname unreachable")

        def getresponse(self) -> object:
            return type("_Resp", (), {"status": 200})()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "dokploy_wizard.packs.headscale.reconciler.http.client.HTTPSConnection",
        FakeConnection,
    )

    assert _http_health_check("https://headscale.example.com/health") is True
    assert calls[0][0] == "headscale.example.com"
    assert calls[1][0] == "127.0.0.1"
    assert calls[1][1] == {"Host": "headscale.example.com"}
    assert calls[1][2] is True


def test_dokploy_headscale_health_prefers_local_container_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployHeadscaleBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name="wizard-stack",
        hostname="headscale.example.com",
        client=FakeDokployApiClient(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale._docker_container_is_up",
        lambda service_name: service_name == "wizard-stack-headscale",
    )

    assert (
        backend.check_health(
            service=HeadscaleResourceRecord(
                resource_id="dokploy-compose:cmp-1:headscale",
                resource_name="wizard-stack-headscale",
            ),
            url="https://headscale.example.com/health",
        )
        is True
    )


def test_dokploy_headscale_health_retries_until_container_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployHeadscaleBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        stack_name="wizard-stack",
        hostname="headscale.example.com",
        client=FakeDokployApiClient(),
    )
    states = iter([False, False, True])
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale._docker_container_is_up",
        lambda service_name: next(states),
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale._http_health_check",
        lambda url: False,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.headscale.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    assert (
        backend.check_health(
            service=HeadscaleResourceRecord(
                resource_id="dokploy-compose:cmp-1:headscale",
                resource_name="wizard-stack-headscale",
            ),
            url="https://headscale.example.com/health",
        )
        is True
    )
    assert sleep_calls == [5.0, 5.0]
