# pyright: reportMissingImports=false

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import pytest

from dokploy_wizard.dokploy.client import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.openclaw import DokployOpenClawBackend, _control_ui_origin_ready
from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
    OpenClawError,
    OpenClawResourceRecord,
    build_my_farm_advisor_ledger,
    build_openclaw_ledger,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.state import (
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
    resolve_desired_state,
)


@dataclass
class FakeOpenClawBackend:
    existing_service: OpenClawResourceRecord | None = None
    health_ok: bool = True
    create_calls: int = 0
    update_calls: int = 0
    last_requested_replicas: int | None = None
    last_health_url: str | None = None

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
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
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self.create_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

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
        del hostname, template_path, variant, channels, secret_refs
        self.update_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service
        self.last_health_url = url
        return self.health_ok


def _decode_seeded_gateway_payload(compose: str) -> dict[str, Any]:
    match = re.search(r"Buffer\.from\('([^']+)',\s*'base64'\)", compose)
    assert match is not None
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


def test_reconcile_openclaw_plans_slot_runtime_for_openclaw_variant() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_GATEWAY_TOKEN": "token-123",
                "OPENCLAW_REPLICAS": "2",
            },
        )
    )

    phase = reconcile_openclaw(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.enabled is True
    assert phase.result.variant == "openclaw"
    assert phase.result.hostname == "openclaw.example.com"
    assert phase.result.channels == ("telegram",)
    assert phase.result.replicas == 2
    assert phase.result.template_path is not None
    assert phase.result.template_path.endswith("templates/packs/openclaw.compose.yaml")
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-advisor"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is None


def test_resolve_desired_state_rejects_matrix_channel_without_matrix_pack() -> None:
    with pytest.raises(StateValidationError, match="Matrix pack"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_OPENCLAW": "true",
                    "OPENCLAW_CHANNELS": "matrix",
                    "OPENCLAW_GATEWAY_TOKEN": "token-123",
                },
            )
        )


def test_resolve_desired_state_rejects_unsupported_advisor_channel() -> None:
    with pytest.raises(StateValidationError, match="Unsupported my-farm-advisor channel"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_MY_FARM_ADVISOR": "true",
                    "MY_FARM_ADVISOR_CHANNELS": "email",
                },
            )
        )


def test_resolve_desired_state_rejects_openclaw_replicas_without_advisor_pack() -> None:
    with pytest.raises(StateValidationError, match="OPENCLAW_REPLICAS requires"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "OPENCLAW_REPLICAS": "2",
                },
            )
        )


def test_resolve_desired_state_rejects_gateway_token_without_openclaw_pack() -> None:
    with pytest.raises(StateValidationError, match="OPENCLAW_GATEWAY_TOKEN requires"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "OPENCLAW_GATEWAY_TOKEN": "token-123",
                },
            )
        )


def test_reconcile_my_farm_advisor_plans_runtime_independently() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
            },
        )
    )

    phase = reconcile_my_farm_advisor(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.variant == "my-farm-advisor"
    assert phase.result.hostname == "farm.example.com"
    assert phase.result.channels == ("matrix", "telegram")
    assert phase.result.template_path is not None
    assert phase.result.template_path.endswith("templates/packs/my-farm-advisor.compose.yaml")
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-my-farm-advisor"


def test_reconcile_my_farm_advisor_uses_healthz_for_health_check() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "MY_FARM_ADVISOR_CHANNELS": "telegram",
            },
        )
    )

    backend = FakeOpenClawBackend()
    phase = reconcile_my_farm_advisor(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.health_check is not None
    assert phase.result.health_check.url == "https://farm.example.com/healthz"
    assert backend.last_health_url == "https://farm.example.com/healthz"


def test_resolve_desired_state_supports_both_advisor_packs_together() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
            },
        )
    )

    assert "openclaw" in desired_state.enabled_packs
    assert "my-farm-advisor" in desired_state.enabled_packs
    assert desired_state.openclaw_channels == ("telegram",)
    assert desired_state.my_farm_advisor_channels == ("matrix", "telegram")


def test_reconcile_openclaw_skips_when_openclaw_is_disabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            },
        )
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False
    assert phase.service_resource_id is None


def test_reconcile_openclaw_reuses_owned_service_and_requires_health() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    backend = FakeOpenClawBackend(
        existing_service=OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name="wizard-stack-advisor",
            replicas=1,
        ),
        health_ok=True,
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
                    resource_id="advisor-service-1",
                    scope="stack:wizard-stack:openclaw",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.service is not None
    assert phase.result.service.action == "update_owned"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.create_calls == 0


def test_reconcile_openclaw_updates_owned_service_when_replicas_change() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_REPLICAS": "3",
            },
        )
    )
    backend = FakeOpenClawBackend(
        existing_service=OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name="wizard-stack-advisor",
            replicas=1,
        ),
        health_ok=True,
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
                    resource_id="advisor-service-1",
                    scope="stack:wizard-stack:openclaw",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.replicas == 3
    assert phase.result.service is not None
    assert phase.result.service.action == "update_owned"
    assert backend.update_calls == 1
    assert backend.last_requested_replicas == 3


def test_reconcile_openclaw_fails_closed_on_unowned_name_collision() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )

    with pytest.raises(OpenClawError, match="requires migration"):
        reconcile_openclaw(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeOpenClawBackend(
                existing_service=OpenClawResourceRecord(
                    resource_id="advisor-service-1",
                    resource_name="wizard-stack-advisor",
                    replicas=1,
                )
            ),
        )


def test_reconcile_my_farm_advisor_reuses_existing_dokploy_managed_service() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "MY_FARM_ADVISOR_CHANNELS": "telegram",
            },
        )
    )

    phase = reconcile_my_farm_advisor(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(
            existing_service=OpenClawResourceRecord(
                resource_id="dokploy-compose:cmp-1:my-farm-advisor:replicas:1",
                resource_name="wizard-stack-my-farm-advisor",
                replicas=1,
            )
        ),
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_existing"


def test_reconcile_openclaw_fails_closed_on_health_check_failure() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )

    with pytest.raises(OpenClawError, match="health check failed"):
        reconcile_openclaw(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeOpenClawBackend(health_ok=False),
        )


def test_build_openclaw_ledger_persists_pack_specific_scope() -> None:
    updated = build_openclaw_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="advisor-service-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
            resource_id="advisor-service-1",
            scope="stack:wizard-stack:openclaw",
        ),
    )


def test_build_my_farm_advisor_ledger_persists_pack_specific_scope() -> None:
    updated = build_my_farm_advisor_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="farm-service-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
            resource_id="farm-service-1",
            scope="stack:wizard-stack:my-farm-advisor",
        ),
    )


@dataclass
class FakeDokployOpenClawApi:
    projects: tuple[DokployProjectSummary, ...] = ()
    created_project: DokployCreatedProject = DokployCreatedProject(
        project_id="project-1",
        environment_id="env-1",
    )
    created_compose_id: str = "compose-1"
    last_create_name: str | None = None
    last_create_compose_file: str | None = None
    last_update_compose_file: str | None = None
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return self.projects

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del name, description, env
        return self.created_project

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del environment_id, app_name
        self.last_create_name = name
        self.last_create_compose_file = compose_file
        return DokployComposeRecord(compose_id=self.created_compose_id, name=name)

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.last_update_compose_file = compose_file
        return DokployComposeRecord(compose_id=compose_id, name="updated")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message=None)


def test_dokploy_openclaw_backend_renders_routable_managed_compose() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        trusted_proxy_emails=("admin@example.com",),
        model_provider="anthropic",
        model_name="claude-3-7-sonnet",
        trusted_proxies="10.0.0.0/8,172.16.0.0/12",
        nvidia_visible_devices="GPU-0",
        client=api,
    )

    record = backend.create_service(
        resource_name="wizard-stack-advisor",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("matrix", "telegram"),
        replicas=2,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert record.resource_name == "wizard-stack-advisor"
    assert record.resource_id == "dokploy-compose:compose-1:openclaw:replicas:2"
    assert "image: ghcr.io/openclaw/openclaw:latest" in compose
    assert "exec node openclaw.mjs gateway --bind lan --port 18789 --allow-unconfigured" in compose
    assert 'ADVISOR_CANONICAL_URL: "https://openclaw.example.com"' in compose
    assert 'CONTROL_UI_ALLOWED_ORIGINS: "https://openclaw.example.com"' in compose
    assert 'TRUSTED_PROXIES: "10.0.0.0/8,172.16.0.0/12"' in compose
    assert 'NVIDIA_VISIBLE_DEVICES: "GPU-0"' in compose
    assert "OPENCLAW_GATEWAY_TOKEN:" not in compose
    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["gateway"]["auth"]["mode"] == "trusted-proxy"
    assert seeded["gateway"]["auth"]["trustedProxy"]["userHeader"] == (
        "cf-access-authenticated-user-email"
    )
    assert seeded["gateway"]["auth"]["trustedProxy"]["requiredHeaders"] == [
        "cf-access-jwt-assertion"
    ]
    assert seeded["gateway"]["auth"]["trustedProxy"]["allowUsers"] == ["admin@example.com"]
    assert seeded["gateway"]["trustedProxies"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert "agents" not in seeded
    assert 'traefik.http.services.wizard-stack-advisor.loadbalancer.server.port: "18789"' in compose
    assert (
        'traefik.http.routers.wizard-stack-advisor.rule: "Host(`openclaw.example.com`)"' in compose
    )
    assert "replicas: 2" in compose


def test_dokploy_openclaw_backend_keeps_token_mode_without_access_emails() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        gateway_token="fixed-token-123",
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-advisor",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert 'OPENCLAW_GATEWAY_TOKEN: "fixed-token-123"' in compose
    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["gateway"]["auth"]["mode"] == "token"
    assert "payload.gateway.auth.token = process.env.OPENCLAW_GATEWAY_TOKEN;" in compose


def test_dokploy_openclaw_backend_uses_explicit_allowed_models_and_provider_keys() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        trusted_proxy_emails=("admin@example.com",),
        openclaw_primary_model="nvidia/moonshotai/kimi-k2.5",
        openclaw_fallback_models=(
            "openrouter/openrouter/free",
            "openrouter/google/gemma-4-31b-it:free",
        ),
        openclaw_openrouter_api_key="or-key",
        openclaw_nvidia_api_key="nv-key",
        openclaw_telegram_bot_token="bot-token",
        openclaw_telegram_owner_user_id="123456789",
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-advisor",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert 'OPENROUTER_API_KEY: "or-key"' in compose
    assert 'NVIDIA_API_KEY: "nv-key"' in compose
    assert 'TELEGRAM_BOT_TOKEN: "bot-token"' in compose
    assert "MODEL_PROVIDER:" not in compose
    assert "MODEL_NAME:" not in compose

    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["agents"]["defaults"]["model"]["primary"] == "nvidia/moonshotai/kimi-k2.5"
    assert seeded["agents"]["defaults"]["model"]["fallbacks"] == [
        "openrouter/openrouter/free",
        "openrouter/google/gemma-4-31b-it:free",
    ]
    assert seeded["agents"]["defaults"]["models"] == {
        "nvidia/moonshotai/kimi-k2.5": {},
        "openrouter/openrouter/free": {},
        "openrouter/google/gemma-4-31b-it:free": {},
    }
    assert seeded["channels"]["telegram"] == {
        "botToken": "bot-token",
        "dmPolicy": "allowlist",
        "allowFrom": ["123456789"],
        "execApprovals": {"enabled": False},
    }


def test_dokploy_openclaw_backend_renders_my_farm_variant_with_same_backend_path() -> None:
    environment = DokployEnvironmentSummary(
        environment_id="env-1",
        name="default",
        is_default=True,
        composes=(
            DokployComposeSummary(
                compose_id="compose-existing",
                name="wizard-stack-my-farm-advisor",
                status="done",
            ),
        ),
    )
    project = DokployProjectSummary(
        project_id="project-1",
        name="wizard-stack",
        environments=(environment,),
    )
    api = FakeDokployOpenClawApi(projects=(project,))
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=api,
    )

    record = backend.update_service(
        resource_id="dokploy-compose:compose-existing:my-farm-advisor:replicas:1",
        resource_name="wizard-stack-my-farm-advisor",
        hostname="farm.example.com",
        template_path=None,
        variant="my-farm-advisor",
        channels=("matrix", "telegram"),
        replicas=3,
        secret_refs=(),
    )

    compose = api.last_update_compose_file
    assert compose is not None
    assert record.resource_id == "dokploy-compose:compose-existing:my-farm-advisor:replicas:3"
    assert "image: ghcr.io/borealbytes/my-farm-advisor:latest" in compose
    assert "node openclaw.mjs gateway --bind lan --port 18789 --allow-unconfigured" in compose
    assert 'ADVISOR_VARIANT: "my-farm-advisor"' in compose
    assert 'ADVISOR_STARTUP_MODE: "my-farm-advisor"' in compose
    assert 'ADVISOR_CHANNELS: "matrix,telegram"' in compose
    assert 'HOME: "/data"' in compose
    assert 'OPENCLAW_STATE_DIR: "/data"' in compose
    assert 'OPENCLAW_WORKSPACE_DIR: "/data/workspace"' in compose
    assert "/data/openclaw.json" in compose
    assert "/data/.openclaw/openclaw.json" in compose
    assert "http://127.0.0.1:18789" in compose
    assert "https://farm.example.com" in compose
    assert 'user: "0:0"' in compose
    assert "wizard-stack-my-farm-advisor-data:/data" in compose
    assert "http://127.0.0.1:18789/healthz" in compose
    assert (
        'traefik.http.routers.wizard-stack-my-farm-advisor.rule: "Host(`farm.example.com`)"'
        in compose
    )
    assert (
        'traefik.http.services.wizard-stack-my-farm-advisor.loadbalancer.server.port: "18789"'
        in compose
    )
    assert 'MODEL_PROVIDER: "openai"' in compose


def test_dokploy_openclaw_backend_accepts_legacy_advisor_service_resource_id() -> None:
    environment = DokployEnvironmentSummary(
        environment_id="env-1",
        name="default",
        is_default=True,
        composes=(
            DokployComposeSummary(
                compose_id="YEGn1TjLLuRP7rPLSh22y",
                name="openmerge-advisor",
                status="done",
            ),
        ),
    )
    project = DokployProjectSummary(
        project_id="project-1",
        name="openmerge",
        environments=(environment,),
    )
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="openmerge",
        client=FakeDokployOpenClawApi(projects=(project,)),
    )

    record = backend.get_service("dokploy-compose:YEGn1TjLLuRP7rPLSh22y:advisor-service")

    assert record is not None
    assert record.resource_id == "dokploy-compose:YEGn1TjLLuRP7rPLSh22y:advisor-service"
    assert record.resource_name == "openmerge-advisor"
    assert record.replicas == 1


def test_control_ui_origin_ready_requires_public_origin_in_seeded_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> Any:
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": '{"gateway":{"controlUi":{"allowedOrigins":["https://openclaw.example.com"]}}}',
            },
        )()

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._find_container_name", lambda service_name: "container-1"
    )
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw.subprocess.run", fake_run)

    assert (
        _control_ui_origin_ready("wizard-stack-advisor", "https://openclaw.example.com/health")
        is True
    )
