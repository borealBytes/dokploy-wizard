# pyright: reportMissingImports=false

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib import error

import pytest

from dokploy_wizard.dokploy.client import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.openclaw import (
    DokployOpenClawBackend,
    _container_name_matches_service,
    _control_ui_origin_ready,
    _local_https_health_check,
    _wait_for_container_http_health,
    _wait_for_docker_container_is_up,
    _wait_for_local_https_health,
)
from dokploy_wizard.lifecycle import classify_modify_request
from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
    OpenClawError,
    OpenClawResourceRecord,
    build_my_farm_advisor_ledger,
    build_openclaw_ledger,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
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
            resource_id="openclaw-service-1",
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
    match = re.search(
        r"Buffer\.from\(\\?[\'\"]([^\'\"]+)\\?[\'\"],\\?[\'\"]base64\\?[\'\"]\)",
        compose,
    )
    assert match is not None
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


def _decode_seeded_gateway_payloads(compose: str) -> list[dict[str, Any]]:
    matches = re.findall(
        r"Buffer\.from\(\\?[\'\"]([^\'\"]+)\\?[\'\"],\\?[\'\"]base64\\?[\'\"]\)",
        compose,
    )
    assert len(matches) >= 2
    return [json.loads(base64.b64decode(item).decode("utf-8")) for item in matches[::2]]


def _decode_extra_seeded_files(compose: str) -> dict[str, str]:
    matches = re.findall(
        r"Buffer\.from\(\\?[\'\"]([^\'\"]+)\\?[\'\"],\\?[\'\"]base64\\?[\'\"]\)",
        compose,
    )
    assert len(matches) >= 2
    payload = json.loads(base64.b64decode(matches[1]).decode("utf-8"))
    return {
        item["path"]: base64.b64decode(item["content"]).decode("utf-8") for item in payload
    }


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>(?:    .*\n)*)",
        compose,
        re.MULTILINE,
    )
    assert match is not None
    return match.group(0)


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
    assert phase.result.service.resource_name == "wizard-stack-openclaw"
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


def test_resolve_desired_state_rejects_nexa_env_without_openclaw_pack() -> None:
    with pytest.raises(StateValidationError, match="require the 'openclaw' pack"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0.example.com",
                },
            )
        )


def test_resolve_desired_state_allows_nexa_env_with_openclaw_pack() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0.example.com",
                "OPENCLAW_NEXA_PRESENCE_POLICY": "rooms-only",
            },
        )
    )

    assert "openclaw" in desired_state.enabled_packs


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
            resource_id="openclaw-service-1",
            resource_name="wizard-stack-openclaw",
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
                    resource_id="openclaw-service-1",
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
            resource_id="openclaw-service-1",
            resource_name="wizard-stack-openclaw",
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
                    resource_id="openclaw-service-1",
                    scope="stack:wizard-stack:openclaw",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.replicas == 3
    assert phase.result.service is not None


def test_modify_nexa_only_env_change_reruns_openclaw_only() -> None:
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "ENABLE_NEXTCLOUD": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0-a.example.com",
        },
    )
    requested_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "ENABLE_NEXTCLOUD": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_NEXA_MEM0_BASE_URL": "https://mem0-b.example.com",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "nextcloud",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCLAW_NEXA_MEM0_BASE_URL" in plan.reasons[0]
    assert plan.start_phase == "openclaw"
    assert plan.phases_to_run == ("openclaw",)


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
                    resource_id="openclaw-service-1",
                    resource_name="wizard-stack-openclaw",
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
        service_resource_id="openclaw-service-1",
        nexa_sidecars_enabled=True,
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
            resource_id="openclaw-service-1",
            scope="stack:wizard-stack:openclaw",
        ),
        OwnedResource(
            resource_type=OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
            resource_id="stack:wizard-stack:openclaw-sidecar:mem0",
            scope="stack:wizard-stack:openclaw-sidecar:mem0",
        ),
        OwnedResource(
            resource_type=OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
            resource_id="stack:wizard-stack:openclaw-sidecar:qdrant",
            scope="stack:wizard-stack:openclaw-sidecar:qdrant",
        ),
        OwnedResource(
            resource_type=OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
            resource_id="stack:wizard-stack:openclaw-sidecar:nexa-runtime",
            scope="stack:wizard-stack:openclaw-sidecar:nexa-runtime",
        ),
    )


def test_build_openclaw_ledger_omits_nexa_sidecars_when_nexa_is_disabled() -> None:
    updated = build_openclaw_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="openclaw-service-1",
        nexa_sidecars_enabled=False,
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
            resource_id="openclaw-service-1",
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
        openclaw_gateway_password="openclaw-ui-generated",
        model_provider="anthropic",
        model_name="claude-3-7-sonnet",
        trusted_proxies="10.0.0.0/8,172.16.0.0/12",
        nvidia_visible_devices="GPU-0",
        client=api,
    )

    record = backend.create_service(
        resource_name="wizard-stack-openclaw",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("matrix", "telegram"),
        replicas=2,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert record.resource_name == "wizard-stack-openclaw"
    assert record.resource_id == "dokploy-compose:compose-1:openclaw:replicas:2"
    assert "image: ghcr.io/openclaw/openclaw:latest" in compose
    assert "exec node openclaw.mjs gateway --bind lan --port 18789 --allow-unconfigured" in compose
    assert 'ADVISOR_CANONICAL_URL: "https://openclaw.example.com"' in compose
    assert 'CONTROL_UI_ALLOWED_ORIGINS: "https://openclaw.example.com"' in compose
    assert 'TRUSTED_PROXIES: "10.0.0.0/8,172.16.0.0/12"' in compose
    assert 'NVIDIA_VISIBLE_DEVICES: "GPU-0"' in compose
    assert "OPENCLAW_GATEWAY_TOKEN:" not in compose
    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["gateway"]["auth"]["mode"] == "token"
    assert seeded["gateway"]["trustedProxies"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert seeded["gateway"]["controlUi"]["allowInsecureAuth"] is True
    assert seeded["discovery"] == {"mdns": {"mode": "off"}}
    assert seeded["meta"] == {"lastTouchedVersion": "dokploy-wizard"}
    assert seeded["tools"] == {
        "profile": "coding",
        "elevated": {
            "enabled": True,
            "allowFrom": {
                "webchat": ["clayton@superiorbyteworks.com"],
                "telegram": [],
                "nextcloud-talk": ["clayton@superiorbyteworks.com", "nexa-agent"],
            },
        },
    }
    assert seeded["agents"]["list"] == [
        {"id": "main", "default": True},
        {
            "id": "telly",
            "name": "Telly",
            "model": {
                "primary": "local/unsloth-active",
                "fallbacks": ["openrouter/auto", "openrouter/openrouter/free"],
            },
            "tools": {
                "profile": "coding",
                "elevated": {
                    "enabled": True,
                    "allowFrom": {
                        "telegram": [],
                        "webchat": ["clayton@superiorbyteworks.com"],
                    },
                },
            },
        },
    ]
    assert seeded["bindings"] == [{"agentId": "telly", "match": {"channel": "telegram"}}]
    assert seeded["agents"]["defaults"] == {
        "elevatedDefault": "off",
        "timeoutSeconds": 300,
        "workspace": "/home/node/.openclaw/workspace",
    }
    assert (
        'traefik.http.services.wizard-stack-openclaw.loadbalancer.server.port: "18789"' in compose
    )
    assert (
        'traefik.http.routers.wizard-stack-openclaw.rule: "Host(`openclaw.example.com`)"' in compose
    )
    assert "wizard-stack-openclaw-data:/home/node/.openclaw" in compose
    assert "  wizard-stack-openclaw-data:" in compose
    assert "replicas: 2" in compose
    assert 'OPENCLAW_DISABLE_BONJOUR: "1"' in compose
    assert 'node -e "fetch(\\"http://127.0.0.1:18789/healthz\\")' in compose
    assert "wget -q -O-" not in compose


def test_dokploy_openclaw_backend_keeps_token_mode_without_access_emails() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        gateway_token="fixed-token-123",
        openclaw_gateway_password="openclaw-ui-generated",
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-openclaw",
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
    assert seeded["gateway"]["remote"]["token"] == "fixed-token-123"
    assert seeded["gateway"]["controlUi"]["allowInsecureAuth"] is True
    assert seeded["discovery"] == {"mdns": {"mode": "off"}}
    assert seeded["tools"] == {
        "profile": "coding",
        "elevated": {
            "enabled": True,
            "allowFrom": {
                "webchat": ["clayton@superiorbyteworks.com"],
                "telegram": [],
                "nextcloud-talk": ["clayton@superiorbyteworks.com", "nexa-agent"],
            },
        },
    }
    assert "payload.gateway.auth.token = process.env.OPENCLAW_GATEWAY_TOKEN;" in compose


def test_dokploy_openclaw_backend_uses_trusted_proxy_mode_for_single_gateway_when_access_emails_present() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        gateway_token="fixed-token-123",
        trusted_proxy_emails=("admin@example.com",),
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-openclaw",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert 'OPENCLAW_GATEWAY_TOKEN: "fixed-token-123"' not in compose
    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["gateway"]["mode"] == "local"
    assert seeded["gateway"]["auth"] == {
        "mode": "trusted-proxy",
        "trustedProxy": {
            "userHeader": "cf-access-authenticated-user-email",
            "allowUsers": ["admin@example.com"],
        },
    }
    assert "remote" not in seeded["gateway"]
    assert "payload.gateway.auth.token = process.env.OPENCLAW_GATEWAY_TOKEN;" not in compose


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
        resource_name="wizard-stack-openclaw",
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
    assert seeded["agents"]["list"] == [
        {"id": "main", "default": True},
        {
            "id": "telly",
            "name": "Telly",
            "model": {
                "primary": "local/unsloth-active",
                "fallbacks": ["openrouter/auto", "openrouter/openrouter/free"],
            },
            "tools": {
                "profile": "coding",
                "elevated": {
                    "enabled": True,
                    "allowFrom": {
                        "telegram": ["123456789"],
                        "webchat": ["clayton@superiorbyteworks.com"],
                    },
                },
            },
        },
    ]
    assert seeded["bindings"] == [{"agentId": "telly", "match": {"channel": "telegram"}}]
    assert seeded["agents"]["defaults"]["models"] == {
        "local/unsloth-active": {},
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


def test_dokploy_openclaw_backend_renders_split_public_and_internal_gateways() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        gateway_token="fixed-token-123",
        openclaw_internal_hostname="openclaw-internal.example.com",
        trusted_proxy_emails=("admin@example.com",),
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-openclaw",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert "  wizard-stack-openclaw:\n" in compose
    assert "  wizard-stack-openclaw-public:\n" in compose
    internal_block = _service_block(compose, "wizard-stack-openclaw")
    public_block = _service_block(compose, "wizard-stack-openclaw-public")
    assert "traefik.enable" not in internal_block
    assert 'OPENCLAW_CONFIG_PATH: "/home/node/.openclaw/openclaw.json"' in internal_block
    assert 'OPENCLAW_CONFIG_PATH: "/home/node/.openclaw-public/openclaw.json"' in public_block
    assert 'OPENCLAW_STATE_DIR: "/home/node/.openclaw-public"' in public_block
    assert "wizard-stack-openclaw-public-data:/home/node/.openclaw-public" in compose
    assert "      - wizard-stack-openclaw" in public_block
    assert 'traefik.http.routers.wizard-stack-openclaw-public.rule: "Host(`openclaw.example.com`)"' in compose

    payloads = _decode_seeded_gateway_payloads(compose)
    assert len(payloads) == 2
    internal_payload, public_payload = payloads
    assert internal_payload["gateway"]["mode"] == "local"
    assert internal_payload["gateway"]["auth"]["mode"] == "token"
    assert internal_payload["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspace"
    assert public_payload["gateway"]["mode"] == "remote"
    assert public_payload["gateway"]["auth"]["mode"] == "trusted-proxy"
    assert public_payload["gateway"]["auth"]["trustedProxy"] == {
        "userHeader": "cf-access-authenticated-user-email",
        "allowUsers": ["admin@example.com"],
    }
    assert public_payload["gateway"]["remote"] == {
        "url": "ws://wizard-stack-openclaw:18789",
        "token": "fixed-token-123",
    }
    assert internal_payload["discovery"] == {"mdns": {"mode": "off"}}
    assert public_payload["discovery"] == {"mdns": {"mode": "off"}}
    assert public_payload["agents"]["defaults"]["workspace"] == "/home/node/.openclaw-public/workspace"
    assert "tools" not in public_payload


def test_dokploy_openclaw_backend_wires_nexa_runtime_contract_and_workspace_surface() -> None:
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        openclaw_nexa_env={
            "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
            "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
            "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
            "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
            "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "qdrant-api-key",
            "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL": "https://nextcloud.example.com",
            "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET": "office-shared-secret",
            "OPENCLAW_NEXA_TALK_SHARED_SECRET": "talk-shared-secret",
            "OPENCLAW_NEXA_TALK_SIGNING_SECRET": "talk-signing-secret",
            "OPENCLAW_NEXA_PRESENCE_POLICY": "rooms-only",
            "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD": "webdav-app-password",
            "OPENCLAW_NEXA_WEBDAV_AUTH_USER": "nexa-agent",
        },
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-openclaw",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    assert 'OPENCLAW_NEXA_DEPLOYMENT_MODE: "sidecar"' in compose
    assert 'OPENCLAW_NEXA_MEM0_BASE_URL: "http://mem0:8000"' in compose
    assert 'OPENCLAW_NEXA_MEM0_VECTOR_BACKEND: "qdrant"' in compose
    assert 'OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL: "http://qdrant:6333"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_ENABLED: "true"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_MEM0_MODE: "rest"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_CREDENTIAL_MEDIATION_MODE: "server-owned-env"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_WORKSPACE_ROOT: "/home/node/.openclaw/workspace/nexa"' in compose
    assert 'DOKPLOY_WIZARD_NEXA_VISIBLE_WORKSPACE_ROOT: "/mnt/openclaw/workspace/nexa"' in compose
    assert "  mem0:\n" in compose
    assert "  qdrant:\n" in compose
    assert "  nexa-runtime:\n" in compose
    assert "wizard-stack-openclaw-qdrant-data:/qdrant/storage" in compose
    assert "wizard-stack-openclaw-mem0-history:/app/history" in compose
    assert "wizard-stack-openclaw-data:/mnt/openclaw" in compose
    assert "traefik.http.routers.mem0" not in compose
    assert "traefik.http.routers.qdrant" not in compose
    assert "traefik.http.routers.nexa-runtime" not in compose
    assert "ports:" not in compose

    mem0_block = _service_block(compose, "mem0")
    qdrant_block = _service_block(compose, "qdrant")
    runtime_block = _service_block(compose, "nexa-runtime")
    assert "dokploy-network" not in mem0_block
    assert "dokploy-network" not in qdrant_block
    assert "dokploy-network" not in runtime_block
    assert "wizard-stack-shared" not in mem0_block
    assert "wizard-stack-shared" not in qdrant_block
    assert "wizard-stack-shared" in runtime_block
    assert "      - default" in mem0_block
    assert "      - default" in qdrant_block
    assert "      - default" in runtime_block
    assert 'image: local/dokploy-wizard-nexa-mem0:latest' in mem0_block
    assert "build:" not in mem0_block
    assert "labels:" not in runtime_block
    assert "expose:" not in runtime_block
    assert 'image: local/dokploy-wizard-nexa-runtime:latest' in runtime_block
    assert "build:" not in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH: "/mnt/openclaw/.nexa/runtime-contract.json"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH: "/mnt/openclaw/workspace/nexa/contract.json"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_STATE_DIR: "/mnt/openclaw/.nexa/state"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_WORKER_MODE: "queue"' in runtime_block
    assert 'DOKPLOY_WIZARD_OPENCLAW_INTERNAL_URL: "http://wizard-stack-openclaw:18789"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_MODEL: "local/unsloth-active"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_MODEL_PROVIDER: "local"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_LOCAL_BASE_URL: "http://tuxdesktop.tailb12aa5.ts.net:61434/v1"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_LOCAL_API_KEY: "sk-no-key-required"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_NVIDIA_BASE_URL: "https://integrate.api.nvidia.com/v1"' in runtime_block
    assert 'DOKPLOY_WIZARD_NEXA_PLANNER_OPENROUTER_BASE_URL: "https://openrouter.ai/api/v1"' in runtime_block
    assert 'OPENCLAW_NEXA_NEXTCLOUD_BASE_URL: "http://wizard-stack-nextcloud"' in runtime_block
    assert 'OPENCLAW_NEXA_MEM0_BASE_URL: "http://mem0:8000"' in runtime_block
    assert 'OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL: "http://qdrant:6333"' in runtime_block
    assert 'OPENCLAW_NEXA_MEM0_API_KEY: "mem0-api-key"' in runtime_block
    assert 'OPENCLAW_NEXA_MEM0_VECTOR_API_KEY: "qdrant-api-key"' in runtime_block
    assert "      - wizard-stack-openclaw" in runtime_block
    assert "      - mem0" in runtime_block
    assert "      - qdrant" in runtime_block

    seeded = _decode_seeded_gateway_payload(compose)
    assert "wizard" not in seeded
    assert seeded["tools"] == {
        "profile": "coding",
        "elevated": {
            "enabled": True,
            "allowFrom": {
                "webchat": ["clayton@superiorbyteworks.com"],
                "telegram": [],
                "nextcloud-talk": ["clayton@superiorbyteworks.com", "nexa-agent"],
            },
        },
    }
    assert seeded["agents"]["list"] == [
        {"id": "main", "default": True},
        {
            "id": "nexa",
            "name": "Nexa",
            "model": {
                "primary": "openrouter/auto",
                "fallbacks": ["openrouter/openrouter/free"],
            },
            "tools": {
                "profile": "coding",
                "elevated": {
                    "enabled": True,
                    "allowFrom": {
                        "nextcloud-talk": ["clayton@superiorbyteworks.com", "nexa-agent"],
                        "webchat": ["clayton@superiorbyteworks.com"],
                    },
                },
            },
        },
        {
            "id": "telly",
            "name": "Telly",
            "model": {
                "primary": "local/unsloth-active",
                "fallbacks": ["openrouter/auto", "openrouter/openrouter/free"],
            },
            "tools": {
                "profile": "coding",
                "elevated": {
                    "enabled": True,
                    "allowFrom": {
                        "telegram": [],
                        "webchat": ["clayton@superiorbyteworks.com"],
                    },
                },
            },
        },
    ]
    assert seeded["bindings"] == [
        {"agentId": "nexa", "match": {"channel": "nextcloud-talk"}},
        {"agentId": "telly", "match": {"channel": "telegram"}},
    ]

    extra_files = _decode_extra_seeded_files(compose)
    assert "/home/node/.openclaw/workspace/SOUL.md" in extra_files
    assert "/home/node/.openclaw/workspace/AGENTS.md" in extra_files
    assert "/home/node/.openclaw/workspace/TOOLS.md" in extra_files
    assert "/home/node/.openclaw/workspace/MCPS.md" in extra_files
    assert "/home/node/.openclaw/workspace/SKILLS.md" in extra_files
    assert "/home/node/.openclaw/workspace-telly/IDENTITY.md" in extra_files
    assert "/home/node/.openclaw/workspace-telly/TOOLS.md" in extra_files
    assert "/home/node/.openclaw/workspace-telly/MCPS.md" in extra_files
    assert "/home/node/.openclaw/workspace-telly/SKILLS.md" in extra_files
    assert "/home/node/.openclaw/.nexa/runtime-contract.json" in extra_files
    assert "/home/node/.openclaw/workspace/nexa/contract.json" in extra_files
    assert "/home/node/.openclaw/workspace/nexa/README.md" in extra_files
    assert "Telegram-facing OpenClaw agent" in extra_files["/home/node/.openclaw/workspace-telly/IDENTITY.md"]
    assert "real tools" in extra_files["/home/node/.openclaw/workspace/TOOLS.md"]
    assert "Context7" in extra_files["/home/node/.openclaw/workspace/MCPS.md"]
    assert "Qdrant" in extra_files["/home/node/.openclaw/workspace-telly/MCPS.md"]
    assert "Qdrant skills" in extra_files["/home/node/.openclaw/workspace/SKILLS.md"]
    assert "shell/file tools" in extra_files["/home/node/.openclaw/workspace-telly/SKILLS.md"]
    workspace_contract = json.loads(extra_files["/home/node/.openclaw/workspace/nexa/contract.json"])
    assert workspace_contract["surface"] == "operator-user-visible"
    assert workspace_contract["authoritative_runtime_state"] == "server-owned env + durable state JSON"
    assert workspace_contract["visible_root"] == "/mnt/openclaw/workspace/nexa"
    assert "mem0-api-key" not in extra_files["/home/node/.openclaw/workspace/nexa/contract.json"]
    assert "nvidia-api-key" not in extra_files["/home/node/.openclaw/workspace/nexa/contract.json"]
    runtime_contract = json.loads(extra_files["/home/node/.openclaw/.nexa/runtime-contract.json"])
    assert runtime_contract["nexa"]["deployment_mode"] == "sidecar"
    assert runtime_contract["topology"] == {
        "mode": "internal-compose-sidecars",
        "internal_network_only": True,
        "runtime_state_dir": "/mnt/openclaw/.nexa/state",
        "services": {"runtime": "nexa-runtime", "mem0": "mem0", "qdrant": "qdrant"},
    }
    assert runtime_contract["credential_mediation"]["secret_env"]["OPENCLAW_NEXA_MEM0_API_KEY"] == {
        "present": True,
        "source": "server-owned-env",
    }
    assert runtime_contract["credential_mediation"]["server_owned_runtime_inputs"] == {
        "nextcloud_base_url": {"present": True, "source": "server-owned-env"},
        "webdav_auth_user": {"present": True, "source": "server-owned-env"},
        "agent_user_id": {"present": True, "source": "server-owned-env"},
        "agent_display_name": {"present": True, "source": "server-owned-env"},
    }
    assert runtime_contract["nextcloud"]["base_url"] == "http://wizard-stack-nextcloud"
    assert runtime_contract["nextcloud"]["webdav"] == {
        "auth_user_present": True,
        "auth_password_present": True,
        "source": "server-owned-env",
    }
    assert runtime_contract["agent_identity"] == {
        "user_id_present": True,
        "display_name_present": True,
        "source": "server-owned-env",
    }
    assert runtime_contract["mem0"]["base_url"] == "http://mem0:8000"
    assert runtime_contract["mem0"]["service_name"] == "mem0"
    assert runtime_contract["mem0"]["vector_base_url"] == "http://qdrant:6333"
    assert runtime_contract["mem0"]["vector_service_name"] == "qdrant"
    assert runtime_contract["mem0"]["require_private_network"] is True
    assert runtime_contract["mem0"]["require_api_key_auth"] is True


def test_dokploy_openclaw_backend_seeds_telly_agent_for_telegram_channel_without_bot_token() -> (
    None
):
    api = FakeDokployOpenClawApi()
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-openclaw",
        hostname="openclaw.example.com",
        template_path=None,
        variant="openclaw",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    compose = api.last_create_compose_file
    assert compose is not None
    seeded = _decode_seeded_gateway_payload(compose)
    assert seeded["agents"]["list"] == [
        {"id": "main", "default": True},
        {
            "id": "telly",
            "name": "Telly",
            "model": {
                "primary": "local/unsloth-active",
                "fallbacks": ["openrouter/auto", "openrouter/openrouter/free"],
            },
            "tools": {
                "profile": "coding",
                "elevated": {
                    "enabled": True,
                    "allowFrom": {
                        "telegram": [],
                        "webchat": ["clayton@superiorbyteworks.com"],
                    },
                },
            },
        },
    ]
    assert seeded["bindings"] == [{"agentId": "telly", "match": {"channel": "telegram"}}]
    assert "channels" not in seeded or "telegram" not in seeded.get("channels", {})


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
                name="openmerge-openclaw",
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
    assert record.resource_name == "openmerge-openclaw"
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
        _control_ui_origin_ready("wizard-stack-openclaw", "https://openclaw.example.com/health")
        is True
    )


def test_openclaw_local_https_health_check_accepts_real_2xx_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeResponse:
        status = 200

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            del exc_type, exc, tb

    class _FakeOpener:
        def open(self, req: Any, timeout: int) -> _FakeResponse:
            captured["url"] = req.full_url
            captured["host"] = req.get_header("Host")
            captured["timeout"] = timeout
            return _FakeResponse()

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw.request.build_opener",
        lambda *handlers: _FakeOpener(),
    )

    assert _local_https_health_check("https://openclaw.example.com/health") is True
    assert captured == {
        "url": "https://127.0.0.1/health",
        "host": "openclaw.example.com",
        "timeout": 15,
    }


@pytest.mark.parametrize(
    ("status", "reason"),
    ((302, "Found"), (403, "Forbidden")),
)
def test_openclaw_local_https_health_check_rejects_access_redirects_and_denials(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reason: str,
) -> None:
    class _FakeOpener:
        def open(self, req: Any, timeout: int) -> Any:
            del timeout
            raise error.HTTPError(req.full_url, status, reason, hdrs=Message(), fp=None)

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw.request.build_opener",
        lambda *handlers: _FakeOpener(),
    )

    assert _local_https_health_check("https://openclaw.example.com/health") is False


def test_dokploy_openclaw_backend_health_fails_when_service_container_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=FakeDokployOpenClawApi(),
    )
    local_probe_calls = 0

    def _unexpected_local_probe(url: str) -> bool:
        del url
        nonlocal local_probe_calls
        local_probe_calls += 1
        return True

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_docker_container_is_up",
        lambda service_name: False,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._local_https_health_check", _unexpected_local_probe
    )

    ok = backend.check_health(
        service=OpenClawResourceRecord(
            resource_id="openclaw-service-1",
            resource_name="wizard-stack-openclaw",
            replicas=1,
        ),
        url="https://openclaw.example.com/health",
    )

    assert ok is False
    assert local_probe_calls == 0


def test_check_health_requires_running_container_even_if_https_probe_would_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=FakeDokployOpenClawApi(),
    )
    service = OpenClawResourceRecord(
        resource_id="dokploy-compose:cmp-1:openclaw:replicas:1",
        resource_name="wizard-stack-openclaw",
        replicas=1,
    )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_docker_container_is_up", lambda _: False
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._local_https_health_check", lambda _: True
    )

    assert backend.check_health(service=service, url="https://openclaw.example.com/health") is False


def test_local_https_health_check_rejects_cloudflare_access_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, req: object, timeout: int) -> object:
            del req, timeout
            raise error.HTTPError(
                url="https://127.0.0.1/health",
                code=403,
                msg="Forbidden",
                hdrs=Message(),
                fp=None,
            )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw.request.build_opener",
        lambda *args: FakeOpener(),
    )

    assert _local_https_health_check("https://openclaw.example.com/health") is False


def test_local_https_health_check_rejects_cloudflare_access_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, req: object, timeout: int) -> object:
            del req, timeout
            raise error.HTTPError(
                url="https://127.0.0.1/health",
                code=302,
                msg="Found",
                hdrs=Message(),
                fp=None,
            )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw.request.build_opener",
        lambda *args: FakeOpener(),
    )

    assert _local_https_health_check("https://openclaw.example.com/health") is False


def test_wait_for_local_https_health_retries_until_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    sleep_calls: list[float] = []

    def fake_probe(url: str) -> bool:
        attempts.append(url)
        return len(attempts) >= 3

    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._local_https_health_check", fake_probe)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw.time.sleep", sleep_calls.append)

    assert _wait_for_local_https_health(
        "https://openclaw.example.com/health", attempts=4, delay_seconds=0.25
    ) is True
    assert attempts == [
        "https://openclaw.example.com/health",
        "https://openclaw.example.com/health",
        "https://openclaw.example.com/health",
    ]
    assert sleep_calls == [0.25, 0.25]


def test_wait_for_local_https_health_hard_fails_after_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    sleep_calls: list[float] = []

    def fake_probe(url: str) -> bool:
        attempts.append(url)
        return False

    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._local_https_health_check", fake_probe)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw.time.sleep", sleep_calls.append)

    assert _wait_for_local_https_health(
        "https://openclaw.example.com/health", attempts=3, delay_seconds=0.5
    ) is False
    assert attempts == [
        "https://openclaw.example.com/health",
        "https://openclaw.example.com/health",
        "https://openclaw.example.com/health",
    ]
    assert sleep_calls == [0.5, 0.5]


def test_wait_for_docker_container_is_up_retries_until_container_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    sleep_calls: list[float] = []

    def fake_container_probe(service_name: str) -> bool:
        attempts.append(service_name)
        return len(attempts) >= 3

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._docker_container_is_up", fake_container_probe
    )
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw.time.sleep", sleep_calls.append)

    assert _wait_for_docker_container_is_up(
        "wizard-stack-openclaw", attempts=4, delay_seconds=0.25
    ) is True
    assert attempts == [
        "wizard-stack-openclaw",
        "wizard-stack-openclaw",
        "wizard-stack-openclaw",
    ]
    assert sleep_calls == [0.25, 0.25]


def test_container_name_match_targets_main_openclaw_service_not_sidecars() -> None:
    assert _container_name_matches_service(
        "openmerge-openclaw-pejp99-openmerge-openclaw-1", "openmerge-openclaw"
    )
    assert not _container_name_matches_service(
        "openmerge-openclaw-pejp99-nexa-runtime-1", "openmerge-openclaw"
    )


def test_wait_for_container_http_health_retries_until_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[str, str, int]] = []
    sleep_calls: list[float] = []

    def fake_probe(service_name: str, url: str, *, app_port: int) -> bool:
        attempts.append((service_name, url, app_port))
        return len(attempts) >= 3

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._container_http_health_check", fake_probe
    )
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw.time.sleep", sleep_calls.append)

    assert _wait_for_container_http_health(
        "wizard-stack-openclaw",
        "https://openclaw.example.com/health",
        app_port=18789,
        attempts=4,
        delay_seconds=0.25,
    ) is True
    assert attempts == [
        ("wizard-stack-openclaw", "https://openclaw.example.com/health", 18789),
        ("wizard-stack-openclaw", "https://openclaw.example.com/health", 18789),
        ("wizard-stack-openclaw", "https://openclaw.example.com/health", 18789),
    ]
    assert sleep_calls == [0.25, 0.25]


def test_check_health_succeeds_when_container_http_probe_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=FakeDokployOpenClawApi(),
    )
    service = OpenClawResourceRecord(
        resource_id="dokploy-compose:cmp-1:openclaw:replicas:1",
        resource_name="wizard-stack-openclaw",
        replicas=1,
    )

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_docker_container_is_up", lambda _: True
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_container_http_health",
        lambda service_name, url, *, app_port: True,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_local_https_health", lambda _: False
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._control_ui_origin_ready", lambda *_: True
    )

    assert backend.check_health(service=service, url="https://openclaw.example.com/health") is True


def test_check_health_falls_back_to_local_https_before_control_ui_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=FakeDokployOpenClawApi(),
    )
    service = OpenClawResourceRecord(
        resource_id="dokploy-compose:cmp-1:openclaw:replicas:1",
        resource_name="wizard-stack-openclaw",
        replicas=1,
    )
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_docker_container_is_up", lambda _: True
    )

    def fake_container_wait(service_name: str, url: str, *, app_port: int) -> bool:
        calls.append(("container", f"{service_name}:{app_port}:{url}"))
        return False

    def fake_wait(url: str) -> bool:
        calls.append(("local", url))
        return True

    def fake_control(service_name: str, url: str) -> bool:
        calls.append((service_name, url))
        return True

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_container_http_health", fake_container_wait
    )
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._wait_for_local_https_health", fake_wait)
    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._control_ui_origin_ready", fake_control)

    assert backend.check_health(service=service, url="https://openclaw.example.com/health") is True
    assert calls == [
        ("container", "wizard-stack-openclaw:18789:https://openclaw.example.com/health"),
        ("local", "https://openclaw.example.com/health"),
        ("wizard-stack-openclaw", "https://openclaw.example.com/health"),
    ]


def test_check_health_fails_when_local_https_never_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        client=FakeDokployOpenClawApi(),
    )
    service = OpenClawResourceRecord(
        resource_id="dokploy-compose:cmp-1:openclaw:replicas:1",
        resource_name="wizard-stack-openclaw",
        replicas=1,
    )
    control_calls = 0

    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_docker_container_is_up", lambda _: True
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_container_http_health",
        lambda service_name, url, *, app_port: False,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.openclaw._wait_for_local_https_health", lambda _: False
    )

    def unexpected_control(*_: object) -> bool:
        nonlocal control_calls
        control_calls += 1
        return True

    monkeypatch.setattr("dokploy_wizard.dokploy.openclaw._control_ui_origin_ready", unexpected_control)

    assert backend.check_health(service=service, url="https://openclaw.example.com/health") is False
    assert control_calls == 0
