# mypy: ignore-errors
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import dokploy_wizard.dokploy.nextcloud as nextcloud_module
from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.dokploy.cloudflared import _render_compose_file as render_cloudflared_compose
from dokploy_wizard.dokploy.coder import _render_compose_file as render_coder_compose
from dokploy_wizard.dokploy.openclaw import DokployOpenClawBackend
from dokploy_wizard.dokploy.seaweedfs import _render_compose_file as render_seaweedfs_compose
from dokploy_wizard.dokploy.shared_core import _render_compose_file as render_shared_core_compose
from dokploy_wizard.state.models import LiteLLMGeneratedKeys

from .test_openclaw_pack import FakeDokployOpenClawApi, _service_environment


@dataclass(frozen=True)
class _RenderedArtifact:
    owner: str
    service_names: tuple[str, ...]
    compose_file: str = field(repr=False)


@dataclass(frozen=True)
class _SentinelSecret:
    key: str
    value: str
    expected_owner: str
    expected_target_services: tuple[str, ...]


_SENTINEL_SECRETS = (
    _SentinelSecret(
        key="CLOUDFLARE_TUNNEL_TOKEN",
        value="SECRET_TEST_CLOUDFLARED_TUNNEL_VALUE",
        expected_owner="cloudflared",
        expected_target_services=("wizard-stack-cloudflared",),
    ),
    _SentinelSecret(
        key="LITELLM_MASTER_KEY",
        value="SECRET_TEST_LITELLM_MASTER_VALUE",
        expected_owner="shared-core",
        expected_target_services=("wizard-stack-shared-litellm",),
    ),
    _SentinelSecret(
        key="LITELLM_SALT_KEY",
        value="SECRET_TEST_LITELLM_SALT_VALUE",
        expected_owner="shared-core",
        expected_target_services=("wizard-stack-shared-litellm",),
    ),
    _SentinelSecret(
        key="NEXTCLOUD_ADMIN_PASSWORD",
        value="SECRET_TEST_NEXTCLOUD_ADMIN_VALUE",
        expected_owner="nextcloud",
        expected_target_services=("wizard-stack-nextcloud",),
    ),
    _SentinelSecret(
        key="SEAWEEDFS_SECRET_KEY",
        value="SECRET_TEST_SEAWEEDFS_SECRET_VALUE",
        expected_owner="seaweedfs",
        expected_target_services=("wizard-stack-seaweedfs",),
    ),
    _SentinelSecret(
        key="OPENCLAW_GATEWAY_PASSWORD",
        value="SECRET_TEST_OPENCLAW_GATEWAY_VALUE",
        expected_owner="openclaw",
        expected_target_services=("wizard-stack-openclaw",),
    ),
    _SentinelSecret(
        key="MY_FARM_ADVISOR_GATEWAY_PASSWORD",
        value="SECRET_TEST_FARM_GATEWAY_VALUE",
        expected_owner="my-farm-advisor",
        expected_target_services=("wizard-stack-my-farm-advisor",),
    ),
    _SentinelSecret(
        key="TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
        value="SECRET_TEST_FIELD_OPERATIONS_BOT_VALUE",
        expected_owner="my-farm-advisor",
        expected_target_services=("wizard-stack-my-farm-advisor",),
    ),
)


def _postgres_allocation(pack_name: str) -> SharedPostgresAllocation:
    normalized = pack_name.replace("-", "_")
    return SharedPostgresAllocation(
        database_name=f"wizard_stack_{normalized}",
        user_name=f"wizard_stack_{normalized}",
        password_secret_ref=f"wizard-stack-{pack_name}-postgres-password",
    )


def _redis_allocation(pack_name: str) -> SharedRedisAllocation:
    normalized = pack_name.replace("-", "_")
    return SharedRedisAllocation(
        identity_name=f"wizard_stack_{normalized}",
        password_secret_ref=f"wizard-stack-{pack_name}-redis-password",
    )


def _litellm_generated_keys() -> LiteLLMGeneratedKeys:
    return LiteLLMGeneratedKeys(
        format_version=1,
        master_key="SECRET_TEST_LITELLM_MASTER_VALUE",
        salt_key="SECRET_TEST_LITELLM_SALT_VALUE",
        virtual_keys={
            "coder-hermes": "SECRET_TEST_CODER_HERMES_VIRTUAL_KEY_VALUE",
            "coder-kdense": "SECRET_TEST_CODER_KDENSE_VIRTUAL_KEY_VALUE",
            "my-farm-advisor": "SECRET_TEST_FARM_VIRTUAL_KEY_VALUE",
            "openclaw": "SECRET_TEST_OPENCLAW_VIRTUAL_KEY_VALUE",
        },
    )


def _render_openclaw_artifact() -> _RenderedArtifact:
    api = FakeDokployOpenClawApi(created_compose_id="compose-openclaw")
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        openclaw_gateway_password="SECRET_TEST_OPENCLAW_GATEWAY_VALUE",
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

    assert api.last_create_compose_file is not None
    return _RenderedArtifact(
        owner="openclaw",
        service_names=("wizard-stack-openclaw",),
        compose_file=api.last_create_compose_file,
    )


def _render_farm_artifact() -> _RenderedArtifact:
    api = FakeDokployOpenClawApi(created_compose_id="compose-farm")
    backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        my_farm_gateway_password="SECRET_TEST_FARM_GATEWAY_VALUE",
        telegram_field_operations_bot_token="SECRET_TEST_FIELD_OPERATIONS_BOT_VALUE",
        client=api,
    )

    backend.create_service(
        resource_name="wizard-stack-my-farm-advisor",
        hostname="farm.example.com",
        template_path=None,
        variant="my-farm-advisor",
        channels=("telegram",),
        replicas=1,
        secret_refs=(),
    )

    assert api.last_create_compose_file is not None
    return _RenderedArtifact(
        owner="my-farm-advisor",
        service_names=("wizard-stack-my-farm-advisor",),
        compose_file=api.last_create_compose_file,
    )


def _representative_artifacts() -> tuple[_RenderedArtifact, ...]:
    shared_plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "nextcloud", "openclaw"),
    )
    nextcloud_postgres = _postgres_allocation("nextcloud")
    nextcloud_redis = _redis_allocation("nextcloud")

    return (
        _RenderedArtifact(
            owner="cloudflared",
            service_names=("wizard-stack-cloudflared",),
            compose_file=render_cloudflared_compose(
                "wizard-stack-cloudflared",
                tunnel_token="SECRET_TEST_CLOUDFLARED_TUNNEL_VALUE",
            ),
        ),
        _RenderedArtifact(
            owner="shared-core",
            service_names=(
                "wizard-stack-shared-postgres",
                "wizard-stack-shared-redis",
                "wizard-stack-shared-litellm",
            ),
            compose_file=render_shared_core_compose(
                shared_plan,
                {},
                {
                    "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
                    "LITELLM_LOCAL_MODEL": "unsloth-active",
                },
                _litellm_generated_keys(),
            ),
        ),
        _RenderedArtifact(
            owner="nextcloud",
            service_names=("wizard-stack-nextcloud", "wizard-stack-onlyoffice"),
            compose_file=nextcloud_module._render_compose_file(
                stack_name="wizard-stack",
                nextcloud_hostname="nextcloud.example.com",
                onlyoffice_hostname="office.example.com",
                postgres_service_name="wizard-stack-shared-postgres",
                redis_service_name="wizard-stack-shared-redis",
                postgres=nextcloud_postgres,
                redis=nextcloud_redis,
                integration_secret_ref="wizard-stack-nextcloud-onlyoffice-jwt-secret",
                admin_user="admin",
                admin_password="SECRET_TEST_NEXTCLOUD_ADMIN_VALUE",
                advisor_workspace_mounts=(),
            ),
        ),
        _RenderedArtifact(
            owner="seaweedfs",
            service_names=("wizard-stack-seaweedfs",),
            compose_file=render_seaweedfs_compose(
                stack_name="wizard-stack",
                hostname="s3.example.com",
                access_key="seaweed-access",
                secret_key="SECRET_TEST_SEAWEEDFS_SECRET_VALUE",
            ),
        ),
        _RenderedArtifact(
            owner="coder",
            service_names=("wizard-stack-coder",),
            compose_file=render_coder_compose(
                stack_name="wizard-stack",
                hostname="coder.example.com",
                wildcard_hostname="*.coder.example.com",
                postgres_service_name="wizard-stack-shared-postgres",
                postgres=_postgres_allocation("coder"),
            ),
        ),
        _render_openclaw_artifact(),
        _render_farm_artifact(),
    )


def _assert_no_raw_secret_values(artifacts: tuple[_RenderedArtifact, ...]) -> None:
    leaks: list[str] = []
    for artifact in artifacts:
        for secret in _SENTINEL_SECRETS:
            if secret.value in artifact.compose_file:
                leaks.append(
                    f"{artifact.owner} compose exposes {secret.key}; expected owner "
                    f"{secret.expected_owner} targets {','.join(secret.expected_target_services)}"
                )

    if leaks:
        pytest.fail("raw secret values found in compose output: " + "; ".join(sorted(leaks)))


def test_representative_wizard_managed_compose_outputs_do_not_inline_raw_secrets() -> None:
    artifacts = _representative_artifacts()

    _assert_no_raw_secret_values(artifacts)


def test_representative_compose_uses_explicit_service_environment_mappings() -> None:
    artifacts_by_owner = {artifact.owner: artifact for artifact in _representative_artifacts()}
    openclaw_env = _service_environment(
        artifacts_by_owner["openclaw"].compose_file,
        "wizard-stack-openclaw",
    )
    farm_env = _service_environment(
        artifacts_by_owner["my-farm-advisor"].compose_file,
        "wizard-stack-my-farm-advisor",
    )

    expected_mappings = {
        "openclaw:OPENCLAW_GATEWAY_PASSWORD": (
            openclaw_env.get("OPENCLAW_GATEWAY_PASSWORD"),
            "${OPENCLAW_GATEWAY_PASSWORD:?OPENCLAW_GATEWAY_PASSWORD is required}",
        ),
        "my-farm-advisor:OPENCLAW_GATEWAY_PASSWORD": (
            farm_env.get("OPENCLAW_GATEWAY_PASSWORD"),
            "${MY_FARM_ADVISOR_GATEWAY_PASSWORD:?MY_FARM_ADVISOR_GATEWAY_PASSWORD is required}",
        ),
        "my-farm-advisor:TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN": (
            farm_env.get("TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN"),
            "${TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN:?TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN is required}",
        ),
    }
    missing_placeholder_mappings = [
        name for name, (actual, expected) in expected_mappings.items() if actual != expected
    ]

    if missing_placeholder_mappings:
        pytest.fail(
            "compose environment entries must use required placeholders for: "
            + ", ".join(sorted(missing_placeholder_mappings))
        )


def test_future_renderer_contract_returns_safe_compose_plus_env_specs() -> None:
    from dokploy_wizard.dokploy.env_spec import RenderedCompose  # type: ignore[import-not-found]

    artifact: Any = _render_openclaw_artifact()

    assert isinstance(artifact, RenderedCompose)
    assert artifact.compose_file
    assert artifact.env_specs
