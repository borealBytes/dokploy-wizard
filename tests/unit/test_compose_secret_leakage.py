# mypy: ignore-errors
# ruff: noqa: E501

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest

import dokploy_wizard.dokploy.docuseal as docuseal_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.dokploy.cloudflared import _render_compose_file as render_cloudflared_compose
from dokploy_wizard.dokploy.coder import _render_compose_file as render_coder_compose
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, RenderedCompose
from dokploy_wizard.dokploy.seaweedfs import _render_compose_file as render_seaweedfs_compose
from dokploy_wizard.dokploy.shared_core import _render_compose_file as render_shared_core_compose
from dokploy_wizard.state.models import LiteLLMGeneratedKeys


@dataclass(frozen=True)
class _RenderedArtifact:
    owner: str
    service_names: tuple[str, ...]
    compose_file: str = field(repr=False)
    env_specs: tuple[DokployEnvSpec, ...] = ()


@dataclass(frozen=True)
class _SentinelSecret:
    key: str
    value: str
    expected_owner: str
    expected_target_services: tuple[str, ...]


_DOCUSEAL_POSTGRES = SharedPostgresAllocation(
    database_name="wizard_stack_docuseal",
    user_name="wizard_stack_docuseal",
    password_secret_ref="wizard-stack-docuseal-postgres-password",
)

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
        # The fixture sets LITELLM_LOCAL_BASE_URL, and the base URL alone runs the
        # autosync sidecar (local endpoint discovery), so the sidecar's master key
        # reference must be covered by the env spec too.
        expected_target_services=("wizard-stack-shared-litellm", "wizard-stack-shared-litellm-sync"),
    ),
    _SentinelSecret(
        key="LITELLM_SALT_KEY",
        value="SECRET_TEST_LITELLM_SALT_VALUE",
        expected_owner="shared-core",
        expected_target_services=("wizard-stack-shared-litellm",),
    ),
    _SentinelSecret(
        key="SEAWEEDFS_SECRET_KEY",
        value="SECRET_TEST_SEAWEEDFS_SECRET_VALUE",
        expected_owner="seaweedfs",
        expected_target_services=("wizard-stack-seaweedfs",),
    ),
    _SentinelSecret(
        key="DOCUSEAL_DATABASE_URL",
        value=docuseal_module._database_url("wizard-stack-shared-postgres", _DOCUSEAL_POSTGRES),
        expected_owner="docuseal",
        expected_target_services=("wizard-stack-docuseal",),
    ),
    _SentinelSecret(
        key="DOCUSEAL_SECRET_KEY_BASE",
        value=docuseal_module._secret_key_base_value(
            "wizard-stack", "wizard-stack-docuseal-secret-key-base"
        ),
        expected_owner="docuseal",
        expected_target_services=("wizard-stack-docuseal",),
    ),
)


def _postgres_allocation(pack_name: str) -> SharedPostgresAllocation:
    normalized = pack_name.replace("-", "_")
    return SharedPostgresAllocation(
        database_name=f"wizard_stack_{normalized}",
        user_name=f"wizard_stack_{normalized}",
        password_secret_ref=f"wizard-stack-{pack_name}-postgres-password",
    )


def _litellm_generated_keys() -> LiteLLMGeneratedKeys:
    return LiteLLMGeneratedKeys(
        format_version=1,
        master_key="SECRET_TEST_LITELLM_MASTER_VALUE",
        salt_key="SECRET_TEST_LITELLM_SALT_VALUE",
        virtual_keys={
            "coder-hermes": "SECRET_TEST_CODER_HERMES_VIRTUAL_KEY_VALUE",
            "coder-kdense": "SECRET_TEST_CODER_KDENSE_VIRTUAL_KEY_VALUE",
            "dokploy-ai": "SECRET_TEST_DOKPLOY_AI_VIRTUAL_KEY_VALUE",
        },
    )


def _artifact_from_rendered(
    *, owner: str, service_names: tuple[str, ...], rendered: RenderedCompose
) -> _RenderedArtifact:
    return _RenderedArtifact(
        owner=owner,
        service_names=service_names,
        compose_file=rendered.compose_file,
        env_specs=rendered.env_specs,
    )


def _representative_artifacts() -> tuple[_RenderedArtifact, ...]:
    shared_plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "docuseal", "matrix"),
    )

    return (
        _artifact_from_rendered(
            owner="cloudflared",
            service_names=("wizard-stack-cloudflared",),
            rendered=render_cloudflared_compose(
                "wizard-stack-cloudflared",
                tunnel_token="SECRET_TEST_CLOUDFLARED_TUNNEL_VALUE",
            ),
        ),
        _artifact_from_rendered(
            owner="shared-core",
            service_names=(
                "wizard-stack-shared-postgres",
                "wizard-stack-shared-redis",
                "wizard-stack-shared-litellm",
            ),
            rendered=render_shared_core_compose(
                shared_plan,
                {},
                {
                    "LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1",
                    "LITELLM_LOCAL_MODEL": "unsloth-active",
                },
                _litellm_generated_keys(),
            ),
        ),
        _artifact_from_rendered(
            owner="seaweedfs",
            service_names=("wizard-stack-seaweedfs",),
            rendered=render_seaweedfs_compose(
                stack_name="wizard-stack",
                hostname="s3.example.com",
                access_key="seaweed-access",
                secret_key="SECRET_TEST_SEAWEEDFS_SECRET_VALUE",
            ),
        ),
        _artifact_from_rendered(
            owner="coder",
            service_names=("wizard-stack-coder",),
            rendered=render_coder_compose(
                stack_name="wizard-stack",
                hostname="coder.example.com",
                wildcard_hostname="*.coder.example.com",
                postgres_service_name="wizard-stack-shared-postgres",
                postgres=_postgres_allocation("coder"),
            ),
        ),
        _artifact_from_rendered(
            owner="docuseal",
            service_names=("wizard-stack-docuseal",),
            rendered=docuseal_module._render_compose_file(
                stack_name="wizard-stack",
                hostname="forms.example.com",
                postgres_service_name="wizard-stack-shared-postgres",
                postgres=_DOCUSEAL_POSTGRES,
                secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base",
            ),
        ),
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


def _assert_env_specs_cover_secret_placeholders(artifacts: tuple[_RenderedArtifact, ...]) -> None:
    failures: list[str] = []
    for artifact in artifacts:
        specs_by_name = {spec.name: spec for spec in artifact.env_specs}
        for secret in _SENTINEL_SECRETS:
            if secret.expected_owner != artifact.owner:
                continue
            placeholder = f"${{{secret.key}:?{secret.key} is required}}"
            if placeholder not in artifact.compose_file:
                continue
            if not artifact.env_specs:
                continue
            spec = specs_by_name.get(secret.key)
            if spec is None:
                failures.append(f"{artifact.owner}:{secret.key} missing env spec")
                continue
            if spec.value != secret.value:
                failures.append(f"{artifact.owner}:{secret.key} env spec does not preserve value")
            if spec.target_services != secret.expected_target_services:
                failures.append(f"{artifact.owner}:{secret.key} target services mismatch")
            if not spec.sensitive:
                failures.append(f"{artifact.owner}:{secret.key} should be sensitive")
    if failures:
        pytest.fail("env specs did not cover required secret placeholders: " + "; ".join(failures))


def test_representative_wizard_managed_compose_outputs_do_not_inline_raw_secrets() -> None:
    artifacts = _representative_artifacts()

    _assert_no_raw_secret_values(artifacts)
    _assert_env_specs_cover_secret_placeholders(artifacts)


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>(?:    .*\n)*)",
        compose,
        re.MULTILINE,
    )
    assert match is not None
    return match.group(0)


def _service_environment(compose: str, service_name: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    capture = False
    for line in _service_block(compose, service_name).splitlines():
        if line == "    environment:":
            capture = True
            continue
        if not capture:
            continue
        if not line.startswith("      "):
            break
        key, raw_value = line.strip().split(": ", 1)
        environment[key] = json.loads(raw_value)
    return environment


def test_representative_compose_uses_explicit_service_environment_mappings() -> None:
    artifacts_by_owner = {artifact.owner: artifact for artifact in _representative_artifacts()}
    shared_core = artifacts_by_owner["shared-core"].compose_file
    docuseal = artifacts_by_owner["docuseal"].compose_file
    postgres_env = _service_environment(shared_core, "wizard-stack-shared-postgres")
    redis_env = _service_environment(shared_core, "wizard-stack-shared-redis")
    litellm_env = _service_environment(shared_core, "wizard-stack-shared-litellm")
    docuseal_env = _service_environment(docuseal, "wizard-stack-docuseal")

    expected_mappings = {
        "shared-core-postgres:POSTGRES_PASSWORD": (
            postgres_env.get("POSTGRES_PASSWORD"),
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}",
        ),
        "shared-core-redis:REDIS_PASSWORD": (
            redis_env.get("REDIS_PASSWORD"),
            "${REDIS_PASSWORD:?REDIS_PASSWORD is required}",
        ),
        "shared-core-litellm:LITELLM_MASTER_KEY": (
            litellm_env.get("LITELLM_MASTER_KEY"),
            "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}",
        ),
        "shared-core-litellm:LITELLM_SALT_KEY": (
            litellm_env.get("LITELLM_SALT_KEY"),
            "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY is required}",
        ),
        "docuseal:DATABASE_URL": (
            docuseal_env.get("DATABASE_URL"),
            "${DOCUSEAL_DATABASE_URL:?DOCUSEAL_DATABASE_URL is required}",
        ),
        "docuseal:SECRET_KEY_BASE": (
            docuseal_env.get("SECRET_KEY_BASE"),
            "${DOCUSEAL_SECRET_KEY_BASE:?DOCUSEAL_SECRET_KEY_BASE is required}",
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

    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=("matrix",))
    artifact: RenderedCompose = render_shared_core_compose(
        plan,
        {},
        {"LITELLM_LOCAL_BASE_URL": "http://vllm.internal:8000/v1"},
        _litellm_generated_keys(),
    )

    assert isinstance(artifact, RenderedCompose)
    assert artifact.compose_file
    assert artifact.env_specs
