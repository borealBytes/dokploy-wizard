# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.dokploy import surfsense_backend as surfsense_backend_module
from dokploy_wizard.dokploy.env_spec import DokployEnvReconciler, DokployEnvSpec
from dokploy_wizard.dokploy.surfsense import (
    SurfSenseBootstrapError,
    SurfSenseHttpResponse,
    SurfSenseReadinessError,
    _render_compose_file,
    ensure_surfsense_first_user_bootstrap,
    render_surfsense_compose_for_state,
)
from dokploy_wizard.lifecycle import applicable_phases_for, classify_modify_request
from dokploy_wizard.packs.surfsense import (
    SURFSENSE_DATA_RESOURCE_TYPE,
    SURFSENSE_SERVICE_RESOURCE_TYPE,
    SurfSenseBootstrapState,
    SurfSenseError,
    SurfSenseResourceRecord,
    build_surfsense_ledger,
    reconcile_surfsense,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ensure_litellm_generated_keys,
    ensure_surfsense_generated_secrets,
    resolve_desired_state,
)
from dokploy_wizard.state.models import (
    LiteLLMGeneratedKeys,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    SurfSenseGeneratedSecrets,
)
from dokploy_wizard.uninstall import build_pack_disable_plan, build_uninstall_plan

_SURFSENSE_SECRETS = SurfSenseGeneratedSecrets(
    format_version=1,
    secrets={
        "db_password": "SECRET_TEST_SURFSENSE_DB_PASSWORD",
        "jwt_secret": "SECRET_TEST_SURFSENSE_JWT_SECRET",
        "searxng_secret": "SECRET_TEST_SURFSENSE_SEARXNG_SECRET",
        "secret_key": "SECRET_TEST_SURFSENSE_SECRET_KEY",
        "zero_admin_password": "SECRET_TEST_SURFSENSE_ZERO_ADMIN_PASSWORD",
    },
)
_LITELLM_KEYS = LiteLLMGeneratedKeys(
    format_version=1,
    master_key="SECRET_TEST_LITELLM_MASTER_KEY",
    salt_key="SECRET_TEST_LITELLM_SALT_KEY",
    virtual_keys={
        "coder-hermes": "SECRET_TEST_CODER_HERMES_VIRTUAL_KEY",
        "coder-kdense": "SECRET_TEST_CODER_KDENSE_VIRTUAL_KEY",
        "dokploy-ai": "SECRET_TEST_DOKPLOY_AI_VIRTUAL_KEY",
        "my-farm-advisor": "SECRET_TEST_FARM_VIRTUAL_KEY",
        "openclaw": "SECRET_TEST_OPENCLAW_VIRTUAL_KEY",
        "surfsense": "SECRET_TEST_SURFSENSE_LITELLM_VIRTUAL_KEY",
    },
)
_FORBIDDEN_UPSTREAM_PROVIDER_KEYS = (
    "LITELLM_OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    "LITELLM_LOCAL_API_KEY",
    "AI_DEFAULT_API_KEY",
)
_FORBIDDEN_UPSTREAM_PROVIDER_VALUES = (
    "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
    "SECRET_TEST_LOCAL_PROVIDER_KEY",
    "SECRET_TEST_AI_DEFAULT_PROVIDER_KEY",
)


_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "SECRET_TEST_SURFSENSE_ADMIN_PASSWORD"
_ACCESS_TOKEN = "SECRET_TEST_SURFSENSE_ACCESS_TOKEN"


@dataclass(frozen=True)
class _RecordedSurfSenseRequest:
    method: str
    hostname: str
    path: str
    payload: dict[str, str] | None


class _FakeSurfSenseHttpClient:
    def __init__(self, responses: list[tuple[str, str, int, object | None]]) -> None:
        self._responses = list(responses)
        self.requests: list[_RecordedSurfSenseRequest] = []

    def get(self, *, hostname: str, path: str) -> SurfSenseHttpResponse:
        self.requests.append(_RecordedSurfSenseRequest("GET", hostname, path, None))
        return self._next("GET", path)

    def post_json(
        self, *, hostname: str, path: str, payload: dict[str, str]
    ) -> SurfSenseHttpResponse:
        self.requests.append(_RecordedSurfSenseRequest("POST_JSON", hostname, path, dict(payload)))
        return self._next("POST_JSON", path)

    def post_form(
        self, *, hostname: str, path: str, form: dict[str, str]
    ) -> SurfSenseHttpResponse:
        self.requests.append(_RecordedSurfSenseRequest("POST_FORM", hostname, path, dict(form)))
        return self._next("POST_FORM", path)

    def _next(self, method: str, path: str) -> SurfSenseHttpResponse:
        assert self._responses, f"No fake response queued for {method} {path}"
        expected_method, expected_path, status, body = self._responses.pop(0)
        assert (method, path) == (expected_method, expected_path)
        if body is None:
            encoded_body = ""
        elif isinstance(body, str):
            encoded_body = body
        else:
            encoded_body = json.dumps(body)
        return SurfSenseHttpResponse(status=status, body=encoded_body)


def test_surfsense_bootstrap_creates_first_user_after_backend_and_frontend_ready() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 200, {"ok": True}),
            ("GET", "/", 200, "<html>SurfSense</html>"),
            ("POST_JSON", "/auth/register", 201, {"id": "user-1", "email": _ADMIN_EMAIL}),
        ]
    )

    result = ensure_surfsense_first_user_bootstrap(
        api_hostname="surfsense-api.example.com",
        frontend_hostname="surfsense.example.com",
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
        http_client=client,
        readiness_attempts=1,
        readiness_delay_seconds=0,
    )

    assert result.created is True
    assert result.verified_existing is False
    assert _ADMIN_EMAIL in result.notes[0]
    assert _ADMIN_PASSWORD not in "\n".join(result.notes)
    assert client.requests == [
        _RecordedSurfSenseRequest("GET", "surfsense-api.example.com", "/ready", None),
        _RecordedSurfSenseRequest("GET", "surfsense.example.com", "/", None),
        _RecordedSurfSenseRequest(
            "POST_JSON",
            "surfsense-api.example.com",
            "/auth/register",
            {"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
        ),
    ]


def test_surfsense_bootstrap_falls_back_to_auth_route_when_ready_returns_404() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 404, {"detail": "Not Found"}),
            ("GET", "/auth/register", 405, {"detail": "Method Not Allowed"}),
            ("GET", "/", 200, "<html>SurfSense</html>"),
            ("POST_JSON", "/auth/register", 201, {"id": "user-1", "email": _ADMIN_EMAIL}),
        ]
    )

    result = ensure_surfsense_first_user_bootstrap(
        api_hostname="surfsense-api.example.com",
        frontend_hostname="surfsense.example.com",
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
        http_client=client,
        readiness_attempts=1,
        readiness_delay_seconds=0,
    )

    assert result.created is True
    assert client.requests[:2] == [
        _RecordedSurfSenseRequest("GET", "surfsense-api.example.com", "/ready", None),
        _RecordedSurfSenseRequest("GET", "surfsense-api.example.com", "/auth/register", None),
    ]


def test_surfsense_bootstrap_ready_404_does_not_pass_without_auth_route() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 404, {"detail": "Not Found"}),
            ("GET", "/auth/register", 404, {"detail": "Not Found"}),
        ]
    )

    with pytest.raises(SurfSenseReadinessError) as exc_info:
        ensure_surfsense_first_user_bootstrap(
            api_hostname="surfsense-api.example.com",
            frontend_hostname="surfsense.example.com",
            admin_email=_ADMIN_EMAIL,
            admin_password=_ADMIN_PASSWORD,
            http_client=client,
            readiness_attempts=1,
            readiness_delay_seconds=0,
        )

    message = str(exc_info.value)
    assert "/ready" in message
    assert "/auth/register" in message
    assert "Last HTTP status: 404" in message
    assert all(request.method != "POST_JSON" for request in client.requests)


def test_surfsense_bootstrap_treats_duplicate_as_idempotent_only_after_login_success() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 200, {"ok": True}),
            ("GET", "/", 307, ""),
            ("POST_JSON", "/auth/register", 400, {"detail": "REGISTER_USER_ALREADY_EXISTS"}),
            (
                "POST_FORM",
                "/auth/jwt/login",
                200,
                {
                    "access_token": _ACCESS_TOKEN,
                    "refresh_token": "SECRET_TEST_SURFSENSE_REFRESH_TOKEN",
                    "token_type": "bearer",
                },
            ),
        ]
    )

    result = ensure_surfsense_first_user_bootstrap(
        api_hostname="surfsense-api.example.com",
        frontend_hostname="surfsense.example.com",
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
        http_client=client,
        readiness_attempts=1,
        readiness_delay_seconds=0,
    )

    assert result.created is False
    assert result.verified_existing is True
    rendered_notes = "\n".join(result.notes)
    assert _ADMIN_EMAIL in rendered_notes
    assert _ADMIN_PASSWORD not in rendered_notes
    assert _ACCESS_TOKEN not in rendered_notes
    assert client.requests[-1] == _RecordedSurfSenseRequest(
        "POST_FORM",
        "surfsense-api.example.com",
        "/auth/jwt/login",
        {
            "username": _ADMIN_EMAIL,
            "password": _ADMIN_PASSWORD,
            "grant_type": "password",
        },
    )


def test_surfsense_bootstrap_duplicate_with_failed_login_is_clear_and_redacted() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 200, {"ok": True}),
            ("GET", "/", 200, "<html>SurfSense</html>"),
            ("POST_JSON", "/auth/register", 400, {"detail": "REGISTER_USER_ALREADY_EXISTS"}),
            (
                "POST_FORM",
                "/auth/jwt/login",
                401,
                {
                    "detail": (
                        "bad password "
                        + _ADMIN_PASSWORD
                        + " access_token="
                        + _ACCESS_TOKEN
                    )
                },
            ),
        ]
    )

    with pytest.raises(SurfSenseBootstrapError) as exc_info:
        ensure_surfsense_first_user_bootstrap(
            api_hostname="surfsense-api.example.com",
            frontend_hostname="surfsense.example.com",
            admin_email=_ADMIN_EMAIL,
            admin_password=_ADMIN_PASSWORD,
            http_client=client,
            readiness_attempts=1,
            readiness_delay_seconds=0,
        )

    message = str(exc_info.value)
    assert "already exists" in message
    assert "different credentials" in message
    assert "HTTP 401" in message
    assert "DOKPLOY_ADMIN_PASSWORD" in message
    assert _ADMIN_PASSWORD not in message
    assert _ACCESS_TOKEN not in message


class _NoopEnvClient:
    def create_compose(self, **kwargs: Any) -> Any:
        raise AssertionError("validate_rendered_compose must not create compose apps")

    def update_compose(self, **kwargs: Any) -> Any:
        raise AssertionError("validate_rendered_compose must not update compose apps")

    def deploy_compose(self, **kwargs: Any) -> Any:
        raise AssertionError("validate_rendered_compose must not deploy compose apps")


def _postgres() -> SharedPostgresAllocation:
    return SharedPostgresAllocation(
        database_name="wizard_stack_surfsense",
        user_name="wizard_stack_surfsense",
        password_secret_ref="wizard-stack-surfsense-postgres-password",
    )


def _redis() -> SharedRedisAllocation:
    return SharedRedisAllocation(
        identity_name="wizard_stack_surfsense",
        password_secret_ref="wizard-stack-surfsense-redis-password",
    )


def _rendered():
    return _render_compose_file(
        stack_name="wizard-stack",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=_postgres(),
        redis=_redis(),
        generated_secrets=_SURFSENSE_SECRETS,
        litellm_generated_keys=_LITELLM_KEYS,
        litellm_model="opencode-go/deepseek-v4-flash",
    )


def _service_names(compose: str) -> tuple[str, ...]:
    names: list[str] = []
    in_services = False
    for line in compose.splitlines():
        if line == "services:":
            in_services = True
            continue
        if in_services and line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_.-]+):$", line)
        if in_services and match:
            names.append(match.group(1))
    return tuple(names)


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>(?:    .*\n|      .*\n|        .*\n|          .*\n)*)",
        compose,
        re.MULTILINE,
    )
    assert match is not None
    return match.group(0)


def _env_specs_by_name(rendered) -> dict[str, DokployEnvSpec]:
    return {spec.name: spec for spec in rendered.env_specs}


def _environment_lines(compose: str) -> tuple[str, ...]:
    lines: list[str] = []
    in_environment = False
    for line in compose.splitlines():
        if line == "    environment:":
            in_environment = True
            continue
        if in_environment and line.startswith("      "):
            lines.append(line)
            continue
        if in_environment:
            in_environment = False
    return tuple(lines)


def test_surfsense_compose_renders_production_services_without_bundled_postgres_or_redis() -> None:
    rendered = _rendered()
    compose = rendered.compose_file

    assert _service_names(compose) == (
        "migrations",
        "searxng",
        "backend",
        "celery_worker",
        "celery_beat",
        "zero-cache",
        "frontend",
    )
    assert "  db:" not in compose
    assert "  postgres:" not in compose
    assert "  redis:" not in compose
    assert "image: pgvector/pgvector" not in compose
    assert "image: postgres:" not in compose
    assert "image: redis:" not in compose
    assert "wizard-stack-shared-postgres" in rendered.env_specs[0].value
    assert "wizard-stack-shared-redis" in _env_specs_by_name(rendered)["SURFSENSE_REDIS_URL"].value

    DokployEnvReconciler(client=_NoopEnvClient()).validate_rendered_compose(rendered)


def test_surfsense_public_routing_is_limited_to_frontend_backend_and_zero_cache() -> None:
    rendered = _rendered()
    compose = rendered.compose_file
    specs = _env_specs_by_name(rendered)
    routed_services = tuple(
        service for service in _service_names(compose) if "traefik.enable" in _service_block(compose, service)
    )

    assert routed_services == ("backend", "zero-cache", "frontend")
    assert "traefik.enable" not in _service_block(compose, "searxng")
    assert "traefik.enable" not in _service_block(compose, "migrations")
    assert "traefik.enable" not in _service_block(compose, "celery_worker")
    assert "traefik.enable" not in _service_block(compose, "celery_beat")
    assert specs["SURFSENSE_FRONTEND_HOSTNAME"].value == "surfsense.example.com"
    assert specs["SURFSENSE_API_HOSTNAME"].value == "surfsense-api.example.com"
    assert specs["SURFSENSE_ZERO_HOSTNAME"].value == "surfsense-zero.example.com"
    assert "Host(`${SURFSENSE_FRONTEND_HOSTNAME:?SURFSENSE_FRONTEND_HOSTNAME is required}`)" in compose
    assert "Host(`${SURFSENSE_API_HOSTNAME:?SURFSENSE_API_HOSTNAME is required}`)" in compose
    assert "Host(`${SURFSENSE_ZERO_HOSTNAME:?SURFSENSE_ZERO_HOSTNAME is required}`)" in compose


def test_surfsense_compose_uses_env_specs_for_generated_secrets_and_litellm_key_without_leaking_values() -> None:
    rendered = _rendered()
    compose = rendered.compose_file
    specs = _env_specs_by_name(rendered)
    raw_secret_values = (
        *_SURFSENSE_SECRETS.secrets.values(),
        _LITELLM_KEYS.virtual_keys["surfsense"],
        _LITELLM_KEYS.master_key,
        _LITELLM_KEYS.salt_key,
        *_FORBIDDEN_UPSTREAM_PROVIDER_VALUES,
    )

    for secret_value in raw_secret_values:
        assert secret_value not in compose

    assert specs["SURFSENSE_DB_PASSWORD"].value == "change-me"
    assert specs["SURFSENSE_DATABASE_URL"].value == (
        "postgresql+asyncpg://wizard_stack_surfsense:change-me"
        "@wizard-stack-shared-postgres:5432/wizard_stack_surfsense"
    )
    assert "sslmode" not in specs["SURFSENSE_DATABASE_URL"].value
    assert specs["SURFSENSE_ZERO_DATABASE_URL"].value == (
        "postgresql://wizard_stack_surfsense:change-me"
        "@wizard-stack-shared-postgres:5432/wizard_stack_surfsense?sslmode=disable"
    )
    assert specs["SURFSENSE_SECRET_KEY"].value == "SECRET_TEST_SURFSENSE_SECRET_KEY"
    assert specs["SURFSENSE_JWT_SECRET"].value == "SECRET_TEST_SURFSENSE_JWT_SECRET"
    assert specs["SURFSENSE_ZERO_ADMIN_PASSWORD"].value == "SECRET_TEST_SURFSENSE_ZERO_ADMIN_PASSWORD"
    assert specs["SURFSENSE_SEARXNG_SECRET"].value == "SECRET_TEST_SURFSENSE_SEARXNG_SECRET"
    assert specs["SURFSENSE_LITELLM_VIRTUAL_KEY"].value == "SECRET_TEST_SURFSENSE_LITELLM_VIRTUAL_KEY"
    assert specs["SURFSENSE_LITELLM_BASE_URL"].value == "http://wizard-stack-shared-litellm:4000"
    assert specs["SURFSENSE_LITELLM_MODEL"].value == "opencode-go/deepseek-v4-flash"
    assert "${SURFSENSE_LITELLM_VIRTUAL_KEY:?SURFSENSE_LITELLM_VIRTUAL_KEY is required}" in compose
    assert "${SURFSENSE_LITELLM_BASE_URL:?SURFSENSE_LITELLM_BASE_URL is required}/v1" in compose

    for forbidden_key in _FORBIDDEN_UPSTREAM_PROVIDER_KEYS:
        assert forbidden_key not in compose
        assert forbidden_key not in specs


def test_surfsense_global_llm_config_exposes_each_allowed_chat_model_without_embedding_or_upstream_keys() -> None:
    rendered = _render_compose_file(
        stack_name="wizard-stack",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=_postgres(),
        redis=_redis(),
        generated_secrets=_SURFSENSE_SECRETS,
        litellm_generated_keys=_LITELLM_KEYS,
        litellm_model="local-model.internal/unsloth-active",
        litellm_models=(
            "local-model.internal/unsloth-active",
            "opencode-go/deepseek-v4-flash",
            "openrouter/hunter-alpha",
        ),
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
    )
    compose = rendered.compose_file
    specs = _env_specs_by_name(rendered)

    assert specs["SURFSENSE_LITELLM_MODEL"].value == "local-model.internal/unsloth-active"
    assert compose.count("provider: OPENAI") == 3
    assert "id: -1" in compose
    assert "id: -2" in compose
    assert "id: -3" in compose
    assert 'name: "LiteLLM - local-model.internal/unsloth-active"' in compose
    assert 'name: "LiteLLM - opencode-go/deepseek-v4-flash"' in compose
    assert 'name: "LiteLLM - openrouter/hunter-alpha"' in compose
    assert "model_name: ${SURFSENSE_LITELLM_MODEL:?SURFSENSE_LITELLM_MODEL is required}" in compose
    assert "model_name: opencode-go/deepseek-v4-flash" in compose
    assert "model_name: openrouter/hunter-alpha" in compose
    assert "Dokploy Wizard LiteLLM" not in compose
    assert "model_name: sentence-transformers/all-MiniLM-L6-v2" not in compose
    assert "SECRET_TEST_SURFSENSE_LITELLM_VIRTUAL_KEY" not in compose
    assert "SECRET_TEST_LITELLM_MASTER_KEY" not in compose
    for forbidden_key in _FORBIDDEN_UPSTREAM_PROVIDER_KEYS:
        assert forbidden_key not in compose
        assert forbidden_key not in specs
    for forbidden_value in _FORBIDDEN_UPSTREAM_PROVIDER_VALUES:
        assert forbidden_value not in compose


def test_surfsense_compose_environment_values_are_required_dokploy_placeholders() -> None:
    rendered = _rendered()
    compose = rendered.compose_file
    specs = _env_specs_by_name(rendered)

    DokployEnvReconciler(client=_NoopEnvClient()).validate_rendered_compose(rendered)
    for line in _environment_lines(compose):
        assert "${SURFSENSE_" in line, line
        placeholder_name = line.split("${", 1)[1].split(":?", 1)[0]
        assert placeholder_name in specs
        assert specs[placeholder_name].required is True

    for expected_name, expected_value in {
        "SURFSENSE_AUTH_TYPE": "LOCAL",
        "SURFSENSE_ETL_SERVICE": "DOCLING",
        "SURFSENSE_EMBEDDING_MODEL": "sentence-transformers/all-MiniLM-L6-v2",
        "SURFSENSE_MIGRATION_TIMEOUT": "900",
        "SURFSENSE_ZERO_QUERY_URL": "http://frontend:3000/api/zero/query",
        "SURFSENSE_FASTAPI_BACKEND_INTERNAL_URL": "http://backend:8000",
    }.items():
        assert specs[expected_name].value == expected_value
        assert specs[expected_name].sensitive is False


def test_surfsense_compose_runs_official_backend_entrypoint_for_each_role() -> None:
    compose = _rendered().compose_file

    expected_roles = {
        "migrations": "SURFSENSE_MIGRATIONS_SERVICE_ROLE",
        "backend": "SURFSENSE_BACKEND_SERVICE_ROLE",
        "celery_worker": "SURFSENSE_CELERY_WORKER_SERVICE_ROLE",
        "celery_beat": "SURFSENSE_CELERY_BEAT_SERVICE_ROLE",
    }
    for service_name, role_env_name in expected_roles.items():
        block = _service_block(compose, service_name)
        assert 'command: ["/app/scripts/docker/entrypoint.sh"]' in block
        assert f"SERVICE_ROLE: \"${{{role_env_name}:?{role_env_name} is required}}\"" in block


def test_surfsense_backend_healthcheck_falls_back_to_mounted_auth_route() -> None:
    backend_block = _service_block(_rendered().compose_file, "backend")

    assert "CMD-SHELL" in backend_block
    assert "http://localhost:8000/ready" in backend_block
    assert "http://localhost:8000/auth/register" in backend_block
    assert "%{http_code}" in backend_block
    assert "405" in backend_block


def test_surfsense_dokploy_backend_health_probe_accepts_auth_route_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_status(host: str, path: str) -> int | None:
        calls.append((host, path))
        return 404 if path == "/ready" else 405

    monkeypatch.setattr(surfsense_backend_module, "_local_https_status", fake_status)

    assert surfsense_backend_module._local_https_health_check("https://surfsense-api.example.com/ready") is True
    assert calls == [
        ("surfsense-api.example.com", "/ready"),
        ("surfsense-api.example.com", "/auth/register"),
    ]


def test_surfsense_dokploy_backend_health_probe_rejects_missing_auth_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_status(host: str, path: str) -> int | None:
        del host, path
        return 404

    monkeypatch.setattr(surfsense_backend_module, "_local_https_status", fake_status)

    assert surfsense_backend_module._local_https_health_check("https://surfsense-api.example.com/ready") is False


def test_surfsense_runtime_overrides_flow_into_env_specs_and_image_tags() -> None:
    rendered = _render_compose_file(
        stack_name="wizard-stack",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=_postgres(),
        redis=_redis(),
        generated_secrets=_SURFSENSE_SECRETS,
        litellm_generated_keys=_LITELLM_KEYS,
        litellm_model="local-model.internal/unsloth-active",
        surfsense_version="0.0.26",
        frontend_public_url="https://research.example.com",
        api_public_url="https://research-api.example.com",
        zero_public_url="https://research-zero.example.com",
        auth_type="OIDC",
        etl_service="UNSTRUCTURED",
        embedding_model="custom-embedding-model",
    )
    specs = _env_specs_by_name(rendered)

    assert "ghcr.io/modsetter/surfsense-backend:0.0.26" in rendered.compose_file
    assert "ghcr.io/modsetter/surfsense-web:0.0.26" in rendered.compose_file
    assert specs["SURFSENSE_FRONTEND_URL"].value == "https://research.example.com"
    assert specs["SURFSENSE_API_URL"].value == "https://research-api.example.com"
    assert specs["SURFSENSE_ZERO_URL"].value == "https://research-zero.example.com"
    assert specs["SURFSENSE_AUTH_TYPE"].value == "OIDC"
    assert specs["SURFSENSE_ETL_SERVICE"].value == "UNSTRUCTURED"
    assert specs["SURFSENSE_EMBEDDING_MODEL"].value == "custom-embedding-model"
    assert specs["SURFSENSE_LITELLM_MODEL"].value == "local-model.internal/unsloth-active"


def test_surfsense_backend_boundary_consumes_generated_state_without_install_env_mutation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    install_env = tmp_path / ".install.env"
    install_env.write_text("STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    original_install_env = install_env.read_text(encoding="utf-8")

    rendered = render_surfsense_compose_for_state(
        stack_name="wizard-stack",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=_postgres(),
        redis=_redis(),
        state_dir=state_dir,
    )
    generated_secrets = ensure_surfsense_generated_secrets(state_dir)
    generated_keys = ensure_litellm_generated_keys(state_dir)
    specs = _env_specs_by_name(rendered)

    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert specs["SURFSENSE_SECRET_KEY"].value == generated_secrets.secrets["secret_key"]
    assert specs["SURFSENSE_LITELLM_VIRTUAL_KEY"].value == generated_keys.virtual_keys["surfsense"]
    assert generated_secrets.secrets["secret_key"] not in rendered.compose_file
    assert generated_keys.virtual_keys["surfsense"] not in rendered.compose_file


def test_surfsense_compose_documents_new_chat_thread_precondition_and_public_api_url() -> None:
    rendered = _rendered()
    frontend_block = _service_block(rendered.compose_file, "frontend")

    assert "POST /api/v1/new_chat requires a chat_id from POST /api/v1/threads" in rendered.compose_file
    assert "404 Thread not found means missing thread state, not route absence" in rendered.compose_file
    assert 'NEXT_PUBLIC_FASTAPI_BACKEND_URL: "${SURFSENSE_API_URL:?SURFSENSE_API_URL is required}"' in frontend_block
    assert "NEXT_PUBLIC_FASTAPI_BACKEND_URL: \"${SURFSENSE_FASTAPI_BACKEND_INTERNAL_URL" not in frontend_block


class _FakeSurfSenseBackend:
    def __init__(
        self,
        *,
        existing_service: SurfSenseResourceRecord | None = None,
        existing_data: SurfSenseResourceRecord | None = None,
        health_ok: bool = True,
        bootstrap_state: SurfSenseBootstrapState | None = None,
    ) -> None:
        self.existing_service = existing_service
        self.existing_data = existing_data
        self.health_ok = health_ok
        self.bootstrap_state = bootstrap_state or SurfSenseBootstrapState(
            created=True,
            verified_existing=False,
        )
        self.created_services: list[dict[str, Any]] = []
        self.updated_services: list[dict[str, Any]] = []
        self.created_data: list[str] = []
        self.health_urls: list[str] = []
        self.bootstrap_calls = 0

    def get_service(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: Any) -> SurfSenseResourceRecord:
        self.created_services.append(dict(kwargs))
        self.existing_service = SurfSenseResourceRecord(
            resource_id="service-1",
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def update_service(self, **kwargs: Any) -> SurfSenseResourceRecord:
        self.updated_services.append(dict(kwargs))
        self.existing_service = SurfSenseResourceRecord(
            resource_id=str(kwargs["resource_id"]),
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def get_persistent_data(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> SurfSenseResourceRecord:
        self.created_data.append(resource_name)
        self.existing_data = SurfSenseResourceRecord(resource_id="data-1", resource_name=resource_name)
        return self.existing_data

    def check_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        self.health_urls.append(url)
        return self.health_ok

    def check_internal_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok

    def ensure_application_ready(self) -> tuple[SurfSenseBootstrapState, tuple[str, ...]]:
        self.bootstrap_calls += 1
        return self.bootstrap_state, ("bootstrapped",)


def _desired_state_for_packs(packs: str):
    return resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "CLOUDFLARE_ACCOUNT_ID": "account-123",
                "CLOUDFLARE_API_TOKEN": "token-123",
                "CLOUDFLARE_ZONE_ID": "zone-123",
                "DOKPLOY_API_KEY": "dokp-test-key",
                "DOKPLOY_API_URL": "https://dokploy.example.com",
                "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
                "DOKPLOY_ADMIN_PASSWORD": "admin-password",
                "PACKS": packs,
                "ROOT_DOMAIN": "example.com",
                "STACK_NAME": "wizard-stack",
            },
        )
    )


def test_surfsense_reconciler_skips_cleanly_when_pack_disabled() -> None:
    backend = _FakeSurfSenseBackend()
    phase = reconcile_surfsense(
        dry_run=False,
        desired_state=_desired_state_for_packs("nextcloud"),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "skipped"
    assert phase.service_resource_id is None
    assert phase.data_resource_id is None
    assert backend.created_services == []
    assert backend.created_data == []


def test_surfsense_reconciler_creates_service_and_ledger_resources_when_enabled() -> None:
    backend = _FakeSurfSenseBackend()
    desired_state = _desired_state_for_packs("surfsense")

    phase = reconcile_surfsense(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    ledger = build_surfsense_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name=desired_state.stack_name,
        service_resource_id=phase.service_resource_id,
        data_resource_id=phase.data_resource_id,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.frontend_hostname == "surfsense.example.com"
    assert phase.result.api_hostname == "surfsense-api.example.com"
    assert phase.result.zero_hostname == "surfsense-zero.example.com"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.created_data == ["wizard-stack-surfsense-data"]
    assert backend.created_services[0]["resource_name"] == "wizard-stack-surfsense"
    assert backend.created_services[0]["postgres_service_name"] == "wizard-stack-shared-postgres"
    assert backend.created_services[0]["redis_service_name"] == "wizard-stack-shared-redis"
    assert [resource.resource_type for resource in ledger.resources] == [
        "surfsense_service",
        "surfsense_data",
    ]


def test_surfsense_desired_state_selection_and_env_spec_contract_rejects_upstream_key_leaks() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "surfsense",
                "LITELLM_OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
                "OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
                "LITELLM_LOCAL_API_KEY": "SECRET_TEST_LOCAL_PROVIDER_KEY",
                "AI_DEFAULT_API_KEY": "SECRET_TEST_AI_DEFAULT_PROVIDER_KEY",
            },
        )
    )
    rendered = _rendered()
    specs = _env_specs_by_name(rendered)

    assert desired_state.enabled_packs == ("surfsense",)
    assert desired_state.hostnames == {
        "dokploy": "dokploy.example.com",
        "surfsense": "surfsense.example.com",
        "surfsense-api": "surfsense-api.example.com",
        "surfsense-zero": "surfsense-zero.example.com",
    }
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == "surfsense"
    )
    assert allocation.postgres is not None
    assert allocation.redis is not None
    assert allocation.postgres.database_name == "wizard_stack_surfsense"
    assert allocation.redis.identity_name == "wizard-stack-surfsense-redis"

    assert specs["SURFSENSE_LITELLM_VIRTUAL_KEY"].owner == "surfsense"
    assert specs["SURFSENSE_LITELLM_VIRTUAL_KEY"].target_services == ()
    assert specs["SURFSENSE_LITELLM_BASE_URL"].sensitive is False
    assert specs["SURFSENSE_LITELLM_MODEL"].value == "opencode-go/deepseek-v4-flash"
    assert specs["SURFSENSE_DATABASE_URL"].target_services == (
        "migrations",
        "backend",
        "celery_worker",
        "celery_beat",
    )
    assert "SURFSENSE_LITELLM_VIRTUAL_KEY" in rendered.compose_file
    assert "SECRET_TEST_SURFSENSE_LITELLM_VIRTUAL_KEY" not in rendered.compose_file
    for forbidden_key in _FORBIDDEN_UPSTREAM_PROVIDER_KEYS:
        assert forbidden_key not in rendered.compose_file
        assert forbidden_key not in specs
    for forbidden_value in _FORBIDDEN_UPSTREAM_PROVIDER_VALUES:
        assert forbidden_value not in rendered.compose_file


def test_surfsense_bootstrap_readiness_failure_is_clear_and_secret_safe() -> None:
    client = _FakeSurfSenseHttpClient(
        [
            ("GET", "/ready", 503, {"detail": "starting " + _ADMIN_PASSWORD}),
        ]
    )

    with pytest.raises(SurfSenseReadinessError) as exc_info:
        ensure_surfsense_first_user_bootstrap(
            api_hostname="surfsense-api.example.com",
            frontend_hostname="surfsense.example.com",
            admin_email=_ADMIN_EMAIL,
            admin_password=_ADMIN_PASSWORD,
            http_client=client,
            readiness_attempts=1,
            readiness_delay_seconds=0,
        )

    message = str(exc_info.value)
    assert "/ready" in message
    assert "Last HTTP status: 503" in message
    assert _ADMIN_PASSWORD not in message


def _dokploy_surfsense_backend(tmp_path: Path, *, stack_name: str = "wizard-stack"):
    return surfsense_backend_module.DokploySurfSenseBackend(
        api_url="https://dokploy.example.com",
        api_key="SECRET_TEST_DOKPLOY_API_KEY",
        state_dir=tmp_path,
        stack_name=stack_name,
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name=f"{stack_name}-shared-postgres",
        redis_service_name=f"{stack_name}-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name=f"{stack_name}_surfsense".replace("-", "_"),
            user_name=f"{stack_name}_surfsense".replace("-", "_"),
            password_secret_ref=f"{stack_name}-surfsense-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name=f"{stack_name}-surfsense-redis",
            password_secret_ref=f"{stack_name}-surfsense-redis-password",
        ),
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
    )


def test_dokploy_backend_bootstrap_uses_extended_readiness_window(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(created=True, verified_existing=False, notes=("bootstrapped",))

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="container-1\twizard-stack-surfsense-dmxwbd-migrations-1\tExited (0) 1 second ago\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(
        surfsense_backend_module,
        "ensure_surfsense_first_user_bootstrap",
        fake_bootstrap,
    )
    monkeypatch.setattr(surfsense_backend_module.subprocess, "run", fake_run)
    backend = surfsense_backend_module.DokploySurfSenseBackend(
        api_url="https://dokploy.example.com",
        api_key="SECRET_TEST_DOKPLOY_API_KEY",
        state_dir=tmp_path,
        stack_name="wizard-stack",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        redis_service_name="wizard-stack-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_surfsense",
            user_name="wizard_stack_surfsense",
            password_secret_ref="wizard-stack-surfsense-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name="wizard-stack-surfsense-redis",
            password_secret_ref="wizard-stack-surfsense-redis-password",
        ),
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
    )

    state, notes = backend.ensure_application_ready()

    assert state == SurfSenseBootstrapState(created=True, verified_existing=False)
    assert notes == ("bootstrapped",)
    assert captured["api_hostname"] == "surfsense-api.example.com"
    assert captured["frontend_hostname"] == "surfsense.example.com"
    assert captured["readiness_attempts"] == 120
    assert captured["readiness_delay_seconds"] == 5.0
    assert surfsense_backend_module._SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS == 360


def test_dokploy_backend_waits_for_migrations_success_before_bootstrap(monkeypatch, tmp_path: Path) -> None:
    statuses = iter(
        [
            "",
            "container-1\topenmerge-surfsense-3hti8i-migrations-1\tCreated\n",
            "container-1\topenmerge-surfsense-3hti8i-migrations-1\tUp 4 seconds (health: starting)\n",
            "container-1\topenmerge-surfsense-3hti8i-migrations-1\tExited (0) 1 second ago\n",
        ]
    )
    bootstrap_calls = 0

    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        del kwargs
        return SimpleNamespace(created=False, verified_existing=True, notes=("ready",))

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=next(statuses), stderr="")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(
        surfsense_backend_module,
        "ensure_surfsense_first_user_bootstrap",
        fake_bootstrap,
    )
    monkeypatch.setattr(surfsense_backend_module.subprocess, "run", fake_run)
    monkeypatch.setattr(surfsense_backend_module.time, "sleep", lambda delay: None)
    backend = _dokploy_surfsense_backend(tmp_path, stack_name="openmerge")

    state, notes = backend.ensure_application_ready()

    assert state == SurfSenseBootstrapState(created=False, verified_existing=True)
    assert notes == ("ready",)
    assert bootstrap_calls == 1


def test_dokploy_backend_bootstrap_surfaces_migration_logs_on_readiness_failure(monkeypatch, tmp_path: Path) -> None:
    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        raise AssertionError("bootstrap should not run after migrations fail")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="container-1\topenmerge-surfsense-dmxwbd-migrations-1\tExited (1) 2 seconds ago\n",
                stderr="",
            )
        if args[:3] == ["docker", "logs", "--tail"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="ERROR: alembic upgrade head failed for SECRET_TEST_SURFSENSE_ADMIN_PASSWORD\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(
        surfsense_backend_module,
        "ensure_surfsense_first_user_bootstrap",
        fake_bootstrap,
    )
    monkeypatch.setattr(surfsense_backend_module.subprocess, "run", fake_run)
    backend = surfsense_backend_module.DokploySurfSenseBackend(
        api_url="https://dokploy.example.com",
        api_key="SECRET_TEST_DOKPLOY_API_KEY",
        state_dir=tmp_path,
        stack_name="openmerge",
        frontend_hostname="surfsense.example.com",
        api_hostname="surfsense-api.example.com",
        zero_hostname="surfsense-zero.example.com",
        postgres_service_name="openmerge-shared-postgres",
        redis_service_name="openmerge-shared-redis",
        postgres=SharedPostgresAllocation(
            database_name="openmerge_surfsense",
            user_name="openmerge_surfsense",
            password_secret_ref="openmerge-surfsense-postgres-password",
        ),
        redis=SharedRedisAllocation(
            identity_name="openmerge-surfsense-redis",
            password_secret_ref="openmerge-surfsense-redis-password",
        ),
        admin_email=_ADMIN_EMAIL,
        admin_password=_ADMIN_PASSWORD,
    )

    with pytest.raises(SurfSenseError) as exc_info:
        backend.ensure_application_ready()

    message = str(exc_info.value)
    assert "migrations container failed" in message
    assert "openmerge-surfsense-dmxwbd-migrations-1" in message
    assert "ERROR: alembic upgrade head failed" in message
    assert _ADMIN_PASSWORD not in message


def test_dokploy_backend_migration_wait_timeout_reports_last_state(monkeypatch, tmp_path: Path) -> None:
    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        raise AssertionError("bootstrap should not run before migrations finish")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="container-1\topenmerge-surfsense-3hti8i-migrations-1\tUp 10 seconds (health: starting)\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(
        surfsense_backend_module,
        "ensure_surfsense_first_user_bootstrap",
        fake_bootstrap,
    )
    monkeypatch.setattr(surfsense_backend_module.subprocess, "run", fake_run)
    monkeypatch.setattr(surfsense_backend_module.time, "sleep", lambda delay: None)
    monkeypatch.setattr(surfsense_backend_module, "_SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS", 2)
    backend = _dokploy_surfsense_backend(tmp_path, stack_name="openmerge")

    with pytest.raises(SurfSenseError) as exc_info:
        backend.ensure_application_ready()

    message = str(exc_info.value)
    assert "did not reach a terminal state" in message
    assert "Up 10 seconds (health: starting)" in message
    assert "/ready" not in message


def test_dokploy_backend_migration_wait_timeout_explains_absent_container_as_deploy_progress(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        raise AssertionError("bootstrap should not run before migrations finish")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(
        surfsense_backend_module,
        "ensure_surfsense_first_user_bootstrap",
        fake_bootstrap,
    )
    monkeypatch.setattr(surfsense_backend_module.subprocess, "run", fake_run)
    monkeypatch.setattr(surfsense_backend_module.time, "sleep", lambda delay: None)
    monkeypatch.setattr(surfsense_backend_module, "_SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS", 2)
    backend = _dokploy_surfsense_backend(tmp_path, stack_name="openmerge")

    with pytest.raises(SurfSenseError) as exc_info:
        backend.ensure_application_ready()

    message = str(exc_info.value)
    assert "migrations container has not been created yet" in message
    assert "Dokploy deployment may still be pulling images or creating containers" in message
    assert "compose.deploy can return before the compose rollout has finished" in message
    assert "/ready" not in message


def test_surfsense_reconciler_reuses_owned_resources_and_reports_inspect_state_shape() -> None:
    desired_state = _desired_state_for_packs("surfsense")
    backend = _FakeSurfSenseBackend(
        existing_service=SurfSenseResourceRecord(
            resource_id="service-owned",
            resource_name="wizard-stack-surfsense",
        ),
        existing_data=SurfSenseResourceRecord(
            resource_id="data-owned",
            resource_name="wizard-stack-surfsense-data",
        ),
        bootstrap_state=SurfSenseBootstrapState(created=False, verified_existing=True),
    )
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                resource_type=SURFSENSE_SERVICE_RESOURCE_TYPE,
                resource_id="service-owned",
                scope="stack:wizard-stack:surfsense:service",
            ),
            OwnedResource(
                resource_type=SURFSENSE_DATA_RESOURCE_TYPE,
                resource_id="data-owned",
                scope="stack:wizard-stack:surfsense:data",
            ),
        ),
    )

    phase = reconcile_surfsense(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=ledger,
        backend=backend,
    )
    payload = phase.result.to_dict()

    assert phase.result.outcome == "already_present"
    assert phase.service_resource_id == "service-owned"
    assert phase.data_resource_id == "data-owned"
    assert backend.created_services == []
    assert backend.created_data == []
    assert backend.updated_services[0]["postgres_service_name"] == "wizard-stack-shared-postgres"
    assert backend.updated_services[0]["redis_service_name"] == "wizard-stack-shared-redis"
    assert backend.bootstrap_calls == 1
    assert payload["service"] == {
        "action": "update_owned",
        "resource_id": "service-owned",
        "resource_name": "wizard-stack-surfsense",
    }
    assert payload["persistent_data"] == {
        "action": "reuse_owned",
        "resource_id": "data-owned",
        "resource_name": "wizard-stack-surfsense-data",
    }
    assert payload["bootstrap_state"] == {"created": False, "verified_existing": True}
    assert payload["health_check"] == {
        "passed": True,
        "path": "/ready",
        "url": "https://surfsense-api.example.com/ready",
    }
    assert payload["config"]["endpoints"] == {
        "frontend_url": "https://surfsense.example.com",
        "api_url": "https://surfsense-api.example.com",
        "zero_url": "https://surfsense-zero.example.com",
    }


def test_surfsense_reconciler_fails_closed_on_owned_service_health_failure() -> None:
    desired_state = _desired_state_for_packs("surfsense")
    backend = _FakeSurfSenseBackend(health_ok=False)

    with pytest.raises(SurfSenseError, match="health check failed"):
        reconcile_surfsense(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


def test_surfsense_lifecycle_modify_and_uninstall_ownership_are_pack_scoped() -> None:
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "surfsense",
        },
    )
    requested_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "surfsense",
            "SURFSENSE_API_SUBDOMAIN": "research-api",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    ledger = build_surfsense_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="service-owned",
        data_resource_id="data-owned",
    )

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
        existing_ledger=ledger,
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )
    disable_plan = build_pack_disable_plan(
        existing_desired=existing_desired,
        requested_desired=resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
            )
        ),
        ownership_ledger=ledger,
    )
    retain_plan = build_uninstall_plan(
        raw_input=existing_raw,
        desired_state=existing_desired,
        ownership_ledger=ledger,
        destroy_data=False,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking", "surfsense")
    assert [item.resource.resource_type for item in disable_plan.deletions] == [
        SURFSENSE_SERVICE_RESOURCE_TYPE,
    ]
    assert [resource.resource_type for resource in disable_plan.retained_resources] == [
        SURFSENSE_DATA_RESOURCE_TYPE,
    ]
    assert disable_plan.completed_steps_ceiling == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
    )
    assert [item.resource.resource_type for item in retain_plan.deletions] == [
        SURFSENSE_SERVICE_RESOURCE_TYPE,
    ]
    assert [resource.resource_type for resource in retain_plan.retained_resources] == [
        SURFSENSE_DATA_RESOURCE_TYPE,
    ]


def test_surfsense_version_modify_schedules_surfsense_phase_without_unsupported_error() -> None:
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "surfsense",
            "SURFSENSE_VERSION": "0.0.25",
        },
    )
    requested_raw = RawEnvInput(
        format_version=1,
        values={
            **existing_raw.values,
            "SURFSENSE_VERSION": "0.0.26",
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
            completed_steps=applicable_phases_for(existing_desired),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "surfsense"
    assert plan.phases_to_run == ("surfsense",)
