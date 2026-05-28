# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy compose renderer for the SurfSense pack."""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar, RenderedCompose
from dokploy_wizard.dokploy.shared_core import _postgres_password_for_allocation
from dokploy_wizard.state import ensure_litellm_generated_keys, ensure_surfsense_generated_secrets
from dokploy_wizard.state.models import LiteLLMGeneratedKeys, SurfSenseGeneratedSecrets
from dokploy_wizard.verification import redact_text

_DEFAULT_SURFSENSE_VERSION = "0.0.25"
_ZERO_IMAGE = "rocicorp/zero:1.4.0"
_SEARXNG_IMAGE = "searxng/searxng:2026.3.13-3c1f68c59"
_DEFAULT_LITELLM_MODEL = "opencode-go/deepseek-v4-flash"
_DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_BACKEND_ENTRYPOINT = "/app/scripts/docker/entrypoint.sh"
_BACKEND_READY_PATH = "/ready"
_BACKEND_ROUTE_READY_FALLBACK_PATH = "/auth/register"
_NEW_CHAT_THREAD_PRECONDITION_NOTE = (
    "SurfSense chat precondition: POST /api/v1/new_chat requires a chat_id from "
    "POST /api/v1/threads; 404 Thread not found means missing thread state, not route absence."
)
_BACKEND_SERVICES = ("migrations", "backend", "celery_worker", "celery_beat")
_SERVICE_ROLE_ENV_NAMES = {
    "migrations": "SURFSENSE_MIGRATIONS_SERVICE_ROLE",
    "backend": "SURFSENSE_BACKEND_SERVICE_ROLE",
    "celery_worker": "SURFSENSE_CELERY_WORKER_SERVICE_ROLE",
    "celery_beat": "SURFSENSE_CELERY_BEAT_SERVICE_ROLE",
}


def render_surfsense_compose_for_state(
    *,
    stack_name: str,
    frontend_hostname: str,
    api_hostname: str,
    zero_hostname: str,
    postgres_service_name: str,
    redis_service_name: str,
    postgres: SharedPostgresAllocation,
    redis: SharedRedisAllocation,
    state_dir: Path,
    litellm_model: str | None = None,
    surfsense_version: str = _DEFAULT_SURFSENSE_VERSION,
    frontend_public_url: str | None = None,
    api_public_url: str | None = None,
    zero_public_url: str | None = None,
    auth_type: str = "LOCAL",
    etl_service: str = "DOCLING",
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    litellm_models: tuple[str, ...] | None = None,
) -> RenderedCompose:
    """Render SurfSense compose using generated state-backed app and LiteLLM secrets."""

    generated_secrets = ensure_surfsense_generated_secrets(state_dir)
    generated_keys = ensure_litellm_generated_keys(state_dir)
    return _render_compose_file(
        stack_name=stack_name,
        frontend_hostname=frontend_hostname,
        api_hostname=api_hostname,
        zero_hostname=zero_hostname,
        postgres_service_name=postgres_service_name,
        redis_service_name=redis_service_name,
        postgres=postgres,
        redis=redis,
        generated_secrets=generated_secrets,
        litellm_generated_keys=generated_keys,
        litellm_model=litellm_model or _DEFAULT_LITELLM_MODEL,
        litellm_models=litellm_models,
        surfsense_version=surfsense_version,
        frontend_public_url=frontend_public_url,
        api_public_url=api_public_url,
        zero_public_url=zero_public_url,
        auth_type=auth_type,
        etl_service=etl_service,
        embedding_model=embedding_model,
    )


@dataclass(frozen=True)
class SurfSenseBootstrapResult:
    """Result of the SurfSense first-user bootstrap hook."""

    created: bool
    verified_existing: bool
    notes: tuple[str, ...]


class SurfSenseBootstrapError(RuntimeError):
    """Raised when SurfSense first-user bootstrap cannot safely complete."""


class SurfSenseReadinessError(SurfSenseBootstrapError):
    """Raised when SurfSense readiness probes do not converge."""


@dataclass(frozen=True)
class SurfSenseHttpResponse:
    status: int
    body: str = ""


class SurfSenseHttpClient(Protocol):
    def get(self, *, hostname: str, path: str) -> SurfSenseHttpResponse: ...

    def post_json(
        self, *, hostname: str, path: str, payload: dict[str, str]
    ) -> SurfSenseHttpResponse: ...

    def post_form(
        self, *, hostname: str, path: str, form: dict[str, str]
    ) -> SurfSenseHttpResponse: ...


def ensure_surfsense_first_user_bootstrap(
    *,
    api_hostname: str,
    frontend_hostname: str | None,
    admin_email: str,
    admin_password: str,
    http_client: SurfSenseHttpClient | None = None,
    readiness_attempts: int = 12,
    readiness_delay_seconds: float = 5.0,
) -> SurfSenseBootstrapResult:
    """Wait for SurfSense and create/verify the first local admin account.

    The caller should pass DOKPLOY_ADMIN_EMAIL and DOKPLOY_ADMIN_PASSWORD as
    admin_email/admin_password. Duplicate registration is idempotent only when a
    follow-up local JWT login succeeds with those same credentials.
    """

    if not admin_email.strip():
        raise SurfSenseBootstrapError("DOKPLOY_ADMIN_EMAIL is required for SurfSense bootstrap.")
    if not admin_password:
        raise SurfSenseBootstrapError("DOKPLOY_ADMIN_PASSWORD is required for SurfSense bootstrap.")
    client = http_client or _LoopbackSurfSenseHttpClient()
    _wait_for_surfsense_backend_ready(
        api_hostname=api_hostname,
        http_client=client,
        attempts=readiness_attempts,
        delay_seconds=readiness_delay_seconds,
    )
    if frontend_hostname:
        _wait_for_surfsense_frontend_reachable(
            frontend_hostname=frontend_hostname,
            http_client=client,
            attempts=readiness_attempts,
            delay_seconds=readiness_delay_seconds,
        )

    registration = client.post_json(
        hostname=api_hostname,
        path="/auth/register",
        payload={"email": admin_email, "password": admin_password},
    )
    if registration.status == 201:
        return SurfSenseBootstrapResult(
            created=True,
            verified_existing=False,
            notes=(f"Created initial SurfSense local admin account for '{admin_email}'.",),
        )
    if _surfsense_duplicate_user_response(registration):
        login_status = _surfsense_login_status(
            api_hostname=api_hostname,
            admin_email=admin_email,
            admin_password=admin_password,
            http_client=client,
        )
        if login_status is None:
            return SurfSenseBootstrapResult(
                created=False,
                verified_existing=True,
                notes=(
                    f"SurfSense local admin account for '{admin_email}' already exists; verified login with DOKPLOY_ADMIN_PASSWORD.",
                ),
            )
        raise SurfSenseBootstrapError(
            "SurfSense local admin account already exists but login with "
            "DOKPLOY_ADMIN_EMAIL/DOKPLOY_ADMIN_PASSWORD failed"
            f" (HTTP {login_status}); the account likely exists with different credentials. "
            "Reset the SurfSense account password or update DOKPLOY_ADMIN_PASSWORD before rerunning bootstrap."
        )
    detail = _safe_surfsense_response_detail(registration, secrets=(admin_password,))
    raise SurfSenseBootstrapError(
        f"SurfSense first-user registration failed with HTTP {registration.status}: {detail}."
    )


def _wait_for_surfsense_backend_ready(
    *,
    api_hostname: str,
    http_client: SurfSenseHttpClient,
    attempts: int,
    delay_seconds: float,
) -> None:
    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(attempts):
        try:
            response = http_client.get(hostname=api_hostname, path=_BACKEND_READY_PATH)
            if response.status == 200:
                return
            last_status = response.status
            last_error = None
            if response.status == 404:
                fallback = http_client.get(
                    hostname=api_hostname,
                    path=_BACKEND_ROUTE_READY_FALLBACK_PATH,
                )
                if fallback.status == 405:
                    return
                last_status = fallback.status
        except SurfSenseBootstrapError as exc:
            last_error = _redact_known_secrets(str(exc), ())
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    suffix = f" Last HTTP status: {last_status}." if last_status is not None else ""
    if last_error:
        suffix = f" Last error: {last_error}."
    raise SurfSenseReadinessError(
        "SurfSense backend /ready did not become reachable before first-user bootstrap, "
        "and the /auth/register route fallback did not prove API routes were mounted."
        + suffix
    )


def _wait_for_surfsense_frontend_reachable(
    *,
    frontend_hostname: str,
    http_client: SurfSenseHttpClient,
    attempts: int,
    delay_seconds: float,
) -> None:
    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(attempts):
        try:
            response = http_client.get(hostname=frontend_hostname, path="/")
            if 200 <= response.status < 400:
                return
            last_status = response.status
            last_error = None
        except SurfSenseBootstrapError as exc:
            last_error = _redact_known_secrets(str(exc), ())
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    suffix = f" Last HTTP status: {last_status}." if last_status is not None else ""
    if last_error:
        suffix = f" Last error: {last_error}."
    raise SurfSenseReadinessError(
        "SurfSense frontend did not become reachable before first-user bootstrap." + suffix
    )


def _surfsense_login_status(
    *,
    api_hostname: str,
    admin_email: str,
    admin_password: str,
    http_client: SurfSenseHttpClient,
) -> int | None:
    response = http_client.post_form(
        hostname=api_hostname,
        path="/auth/jwt/login",
        form={
            "username": admin_email,
            "password": admin_password,
            "grant_type": "password",
        },
    )
    if response.status not in {200, 201}:
        return response.status
    payload = _surfsense_json_payload(response)
    if not isinstance(payload, dict):
        return response.status
    token = payload.get("access_token")
    token_type = payload.get("token_type")
    if isinstance(token, str) and token and isinstance(token_type, str) and token_type:
        return None
    return response.status


def _surfsense_duplicate_user_response(response: SurfSenseHttpResponse) -> bool:
    if response.status != 400:
        return False
    payload = _surfsense_json_payload(response)
    if payload is None:
        haystack = response.body
    else:
        haystack = json.dumps(payload, sort_keys=True)
    return "REGISTER_USER_ALREADY_EXISTS" in haystack


def _safe_surfsense_response_detail(
    response: SurfSenseHttpResponse, *, secrets: tuple[str, ...]
) -> str:
    payload = _surfsense_json_payload(response)
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _redact_known_secrets(value, secrets)
    return "response detail unavailable (redacted)"


def _surfsense_json_payload(response: SurfSenseHttpResponse) -> object | None:
    if not response.body.strip():
        return None
    try:
        return json.loads(response.body)
    except json.JSONDecodeError:
        return None


def _redact_known_secrets(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<REDACTED>")
    return redact_text(redacted)


class _LoopbackSurfSenseHttpClient:
    def get(self, *, hostname: str, path: str) -> SurfSenseHttpResponse:
        return self._request(hostname=hostname, method="GET", path=path)

    def post_json(
        self, *, hostname: str, path: str, payload: dict[str, str]
    ) -> SurfSenseHttpResponse:
        return self._request(
            hostname=hostname,
            method="POST",
            path=path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def post_form(
        self, *, hostname: str, path: str, form: dict[str, str]
    ) -> SurfSenseHttpResponse:
        return self._request(
            hostname=hostname,
            method="POST",
            path=path,
            data=parse.urlencode(form).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def _request(
        self,
        *,
        hostname: str,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> SurfSenseHttpResponse:
        request_headers = {
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Host": hostname,
        }
        if headers is not None:
            request_headers.update(headers)
        req = urlrequest.Request(
            f"https://127.0.0.1{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        context = ssl._create_unverified_context()
        try:
            with urlrequest.urlopen(req, timeout=20, context=context) as response:  # noqa: S310
                return SurfSenseHttpResponse(
                    status=response.status,
                    body=response.read().decode("utf-8", "ignore"),
                )
        except urlerror.HTTPError as exc:
            return SurfSenseHttpResponse(
                status=exc.code,
                body=exc.read().decode("utf-8", "ignore"),
            )
        except (urlerror.URLError, OSError, TimeoutError) as exc:
            raise SurfSenseBootstrapError(
                f"SurfSense request {method} {path} to '{hostname}' failed: {_redact_known_secrets(str(exc), ())}."
            ) from exc


def _render_compose_file(
    *,
    stack_name: str,
    frontend_hostname: str,
    api_hostname: str,
    zero_hostname: str,
    postgres_service_name: str,
    redis_service_name: str,
    postgres: SharedPostgresAllocation,
    redis: SharedRedisAllocation,
    generated_secrets: SurfSenseGeneratedSecrets | None = None,
    litellm_generated_keys: LiteLLMGeneratedKeys | None = None,
    litellm_model: str = _DEFAULT_LITELLM_MODEL,
    litellm_models: tuple[str, ...] | None = None,
    surfsense_version: str = _DEFAULT_SURFSENSE_VERSION,
    frontend_public_url: str | None = None,
    api_public_url: str | None = None,
    zero_public_url: str | None = None,
    auth_type: str = "LOCAL",
    etl_service: str = "DOCLING",
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
) -> RenderedCompose:
    secrets = _surfsense_secrets(generated_secrets)
    litellm_virtual_key = _litellm_virtual_key_value(litellm_generated_keys)
    litellm_model_aliases = _surfsense_litellm_model_aliases(
        primary_model=litellm_model,
        models=litellm_models,
    )
    shared_network = _shared_network_name(stack_name)
    postgres_password = _postgres_password_for_allocation(postgres)
    database_url = _database_url(postgres_service_name, postgres, postgres_password)
    zero_database_url = _zero_database_url(
        postgres_service_name,
        postgres,
        postgres_password,
    )
    redis_url = _redis_url(redis_service_name)
    frontend_url = _non_empty_or_default(frontend_public_url, f"https://{frontend_hostname}")
    api_url = _non_empty_or_default(api_public_url, f"https://{api_hostname}")
    zero_url = _non_empty_or_default(zero_public_url, f"https://{zero_hostname}")
    litellm_base_url = _litellm_internal_base_url(stack_name)
    backend_image = _surfsense_backend_image(surfsense_version)
    web_image = _surfsense_web_image(surfsense_version)

    env_specs = (
        _env_spec(
            name="SURFSENSE_DATABASE_URL",
            value=database_url,
            target_services=_BACKEND_SERVICES,
            source="surfsense-shared-postgres-url",
        ),
        _env_spec(
            name="SURFSENSE_ZERO_DATABASE_URL",
            value=zero_database_url,
            target_services=("zero-cache",),
            source="surfsense-shared-postgres-zero-url",
        ),
        _env_spec(
            name="SURFSENSE_REDIS_URL",
            value=redis_url,
            target_services=_BACKEND_SERVICES,
            source=redis.password_secret_ref,
        ),
        _env_spec(
            name="SURFSENSE_SECRET_KEY",
            value=secrets["secret_key"],
            target_services=_BACKEND_SERVICES,
            source="surfsense-generated-secret-key",
        ),
        _env_spec(
            name="SURFSENSE_JWT_SECRET",
            value=secrets["jwt_secret"],
            target_services=_BACKEND_SERVICES,
            source="surfsense-generated-jwt-secret",
        ),
        _env_spec(
            name="SURFSENSE_DB_PASSWORD",
            value=postgres_password,
            target_services=_BACKEND_SERVICES + ("zero-cache",),
            source=postgres.password_secret_ref,
        ),
        _env_spec(
            name="SURFSENSE_SEARXNG_SECRET",
            value=secrets["searxng_secret"],
            target_services=("searxng",),
            source="surfsense-generated-searxng-secret",
        ),
        _env_spec(
            name="SURFSENSE_ZERO_ADMIN_PASSWORD",
            value=secrets["zero_admin_password"],
            target_services=("zero-cache",),
            source="surfsense-generated-zero-admin-password",
        ),
        _env_spec(
            name="SURFSENSE_LITELLM_VIRTUAL_KEY",
            value=litellm_virtual_key,
            target_services=(),
            source="generated-litellm:surfsense",
        ),
        _env_spec(
            name="SURFSENSE_LITELLM_BASE_URL",
            value=litellm_base_url,
            target_services=(),
            source="shared-core-litellm",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_LITELLM_MODEL",
            value=litellm_model_aliases[0],
            target_services=(),
            source="surfsense-litellm-model",
            sensitive=False,
        ),
        _runtime_env_spec(
            name="SURFSENSE_AUTH_TYPE",
            value=auth_type,
            target_services=_BACKEND_SERVICES + ("frontend",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ETL_SERVICE",
            value=etl_service,
            target_services=_BACKEND_SERVICES + ("frontend",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_EMBEDDING_MODEL",
            value=embedding_model,
            target_services=_BACKEND_SERVICES,
        ),
        _runtime_env_spec(
            name="SURFSENSE_CELERY_TASK_DEFAULT_QUEUE",
            value="surfsense",
            target_services=_BACKEND_SERVICES,
        ),
        _runtime_env_spec(
            name="SURFSENSE_PYTHONPATH",
            value="/app",
            target_services=_BACKEND_SERVICES,
        ),
        _runtime_env_spec(
            name="SURFSENSE_MIGRATIONS_SERVICE_ROLE",
            value="migrate",
            target_services=("migrations",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_BACKEND_SERVICE_ROLE",
            value="api",
            target_services=("backend",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_CELERY_WORKER_SERVICE_ROLE",
            value="worker",
            target_services=("celery_worker",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_CELERY_BEAT_SERVICE_ROLE",
            value="beat",
            target_services=("celery_beat",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_MIGRATION_TIMEOUT",
            value="900",
            target_services=("migrations",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_SEARXNG_DEFAULT_HOST",
            value="http://searxng:8080",
            target_services=("backend", "celery_worker"),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_REPLICA_FILE",
            value="/data/zero.db",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_APP_PUBLICATIONS",
            value="zero_publication",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_NUM_SYNC_WORKERS",
            value="4",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_UPSTREAM_MAX_CONNS",
            value="20",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_CVR_MAX_CONNS",
            value="30",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_QUERY_URL",
            value="http://frontend:3000/api/zero/query",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_ZERO_MUTATE_URL",
            value="http://frontend:3000/api/zero/mutate",
            target_services=("zero-cache",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_FRONTEND_DEPLOYMENT_MODE",
            value="self-hosted",
            target_services=("frontend",),
        ),
        _runtime_env_spec(
            name="SURFSENSE_FASTAPI_BACKEND_INTERNAL_URL",
            value="http://backend:8000",
            target_services=("frontend",),
        ),
        _env_spec(
            name="SURFSENSE_FRONTEND_URL",
            value=frontend_url,
            target_services=("backend",),
            source="hostname-plan",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_FRONTEND_HOSTNAME",
            value=frontend_hostname,
            target_services=("frontend",),
            source="hostname-plan",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_API_URL",
            value=api_url,
            target_services=("backend", "frontend"),
            source="hostname-plan",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_API_HOSTNAME",
            value=api_hostname,
            target_services=("backend",),
            source="hostname-plan",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_ZERO_URL",
            value=zero_url,
            target_services=("frontend", "zero-cache"),
            source="hostname-plan",
            sensitive=False,
        ),
        _env_spec(
            name="SURFSENSE_ZERO_HOSTNAME",
            value=zero_hostname,
            target_services=("zero-cache",),
            source="hostname-plan",
            sensitive=False,
        ),
    )

    compose_file = (
        "services:\n"
        f"{_migrations_service(shared_network, backend_image=backend_image)}"
        f"{_searxng_service()}"
        f"{_backend_service(shared_network, backend_image=backend_image)}"
        f"{_celery_worker_service(shared_network, backend_image=backend_image)}"
        f"{_celery_beat_service(shared_network, backend_image=backend_image)}"
        f"{_zero_cache_service(shared_network)}"
        f"{_frontend_service(web_image=web_image)}"
        "volumes:\n"
        "  surfsense-shared-temp:\n"
        "  surfsense-zero-cache:\n"
        "  surfsense-zero-init:\n"
        "configs:\n"
        f"{_global_llm_config(stack_name, litellm_model_aliases)}"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        f"    name: {shared_network}\n"
        "    external: true\n"
        f"# SurfSense public endpoints: frontend={frontend_url} api={api_url} zero={zero_url}\n"
        f"# {_NEW_CHAT_THREAD_PRECONDITION_NOTE}\n"
        f"# SurfSense shared bindings: postgres={postgres_service_name} redis={redis_service_name} litellm={stack_name}-shared-litellm\n"
    )
    return RenderedCompose(compose_file=compose_file, env_specs=env_specs)


def _migrations_service(shared_network: str, *, backend_image: str) -> str:
    return (
        "  migrations:\n"
        f"    image: {backend_image}\n"
        "    restart: \"no\"\n"
        f"    command: [{_quote(_BACKEND_ENTRYPOINT)}]\n"
        "    environment:\n"
        f"{_backend_common_env(service_name='migrations')}"
        f"      MIGRATION_TIMEOUT: {_quote(_required_placeholder('SURFSENSE_MIGRATION_TIMEOUT'))}\n"
        "    volumes:\n"
        "      - surfsense-zero-init:/zero-init\n"
        "    configs:\n"
        "      - source: surfsense-global-llm-config\n"
        "        target: /app/app/config/global_llm_config.yaml\n"
        "    networks:\n"
        "      - default\n"
        f"      - {shared_network}\n"
    )


def _searxng_service() -> str:
    return (
        "  searxng:\n"
        f"    image: {_SEARXNG_IMAGE}\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      SEARXNG_SECRET: {_quote(_required_placeholder('SURFSENSE_SEARXNG_SECRET'))}\n"
        "    healthcheck:\n"
        "      test: [\"CMD\", \"wget\", \"--spider\", \"-q\", \"http://localhost:8080/healthz\"]\n"
        "      interval: 10s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
    )


def _backend_service(shared_network: str, *, backend_image: str) -> str:
    return (
        "  backend:\n"
        f"    image: {backend_image}\n"
        "    restart: unless-stopped\n"
        f"    command: [{_quote(_BACKEND_ENTRYPOINT)}]\n"
        "    expose:\n"
        "      - '8000'\n"
        "    extra_hosts:\n"
        "      - \"host.docker.internal:host-gateway\"\n"
        "    environment:\n"
        f"{_backend_common_env(service_name='backend')}"
        f"      NEXT_FRONTEND_URL: {_quote(_required_placeholder('SURFSENSE_FRONTEND_URL'))}\n"
        f"      BACKEND_URL: {_quote(_required_placeholder('SURFSENSE_API_URL'))}\n"
        f"      SEARXNG_DEFAULT_HOST: {_quote(_required_placeholder('SURFSENSE_SEARXNG_DEFAULT_HOST'))}\n"
        "    labels:\n"
        f"{_traefik_labels('backend', 'SURFSENSE_API_HOSTNAME', 8000)}"
        "    depends_on:\n"
        "      searxng:\n"
        "        condition: service_healthy\n"
        "      migrations:\n"
        "        condition: service_completed_successfully\n"
        "    healthcheck:\n"
        "      test: [\"CMD-SHELL\", \"curl -fsS http://localhost:8000/ready || [ \\\"$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/auth/register)\\\" = \\\"405\\\" ]\"]\n"
        "      interval: 15s\n"
        "      timeout: 5s\n"
        "      retries: 30\n"
        "      start_period: 200s\n"
        "    volumes:\n"
        "      - surfsense-shared-temp:/shared_tmp\n"
        "    configs:\n"
        "      - source: surfsense-global-llm-config\n"
        "        target: /app/app/config/global_llm_config.yaml\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
    )


def _celery_worker_service(shared_network: str, *, backend_image: str) -> str:
    return (
        "  celery_worker:\n"
        f"    image: {backend_image}\n"
        "    restart: unless-stopped\n"
        f"    command: [{_quote(_BACKEND_ENTRYPOINT)}]\n"
        "    extra_hosts:\n"
        "      - \"host.docker.internal:host-gateway\"\n"
        "    environment:\n"
        f"{_backend_common_env(service_name='celery_worker')}"
        f"      SEARXNG_DEFAULT_HOST: {_quote(_required_placeholder('SURFSENSE_SEARXNG_DEFAULT_HOST'))}\n"
        "    depends_on:\n"
        "      migrations:\n"
        "        condition: service_completed_successfully\n"
        "      backend:\n"
        "        condition: service_healthy\n"
        "      searxng:\n"
        "        condition: service_healthy\n"
        "    volumes:\n"
        "      - surfsense-shared-temp:/shared_tmp\n"
        "    configs:\n"
        "      - source: surfsense-global-llm-config\n"
        "        target: /app/app/config/global_llm_config.yaml\n"
        "    networks:\n"
        "      - default\n"
        f"      - {shared_network}\n"
    )


def _celery_beat_service(shared_network: str, *, backend_image: str) -> str:
    return (
        "  celery_beat:\n"
        f"    image: {backend_image}\n"
        "    restart: unless-stopped\n"
        f"    command: [{_quote(_BACKEND_ENTRYPOINT)}]\n"
        "    environment:\n"
        f"{_backend_common_env(service_name='celery_beat')}"
        "    depends_on:\n"
        "      migrations:\n"
        "        condition: service_completed_successfully\n"
        "      celery_worker:\n"
        "        condition: service_started\n"
        "    configs:\n"
        "      - source: surfsense-global-llm-config\n"
        "        target: /app/app/config/global_llm_config.yaml\n"
        "    networks:\n"
        "      - default\n"
        f"      - {shared_network}\n"
    )


def _zero_cache_service(shared_network: str) -> str:
    return (
        "  zero-cache:\n"
        f"    image: {_ZERO_IMAGE}\n"
        "    restart: unless-stopped\n"
        "    expose:\n"
        "      - '4848'\n"
        "    extra_hosts:\n"
        "      - \"host.docker.internal:host-gateway\"\n"
        "    environment:\n"
        f"      ZERO_UPSTREAM_DB: {_quote(_required_placeholder('SURFSENSE_ZERO_DATABASE_URL'))}\n"
        f"      ZERO_CVR_DB: {_quote(_required_placeholder('SURFSENSE_ZERO_DATABASE_URL'))}\n"
        f"      ZERO_CHANGE_DB: {_quote(_required_placeholder('SURFSENSE_ZERO_DATABASE_URL'))}\n"
        f"      ZERO_REPLICA_FILE: {_quote(_required_placeholder('SURFSENSE_ZERO_REPLICA_FILE'))}\n"
        f"      ZERO_ADMIN_PASSWORD: {_quote(_required_placeholder('SURFSENSE_ZERO_ADMIN_PASSWORD'))}\n"
        f"      ZERO_APP_PUBLICATIONS: {_quote(_required_placeholder('SURFSENSE_ZERO_APP_PUBLICATIONS'))}\n"
        f"      ZERO_NUM_SYNC_WORKERS: {_quote(_required_placeholder('SURFSENSE_ZERO_NUM_SYNC_WORKERS'))}\n"
        f"      ZERO_UPSTREAM_MAX_CONNS: {_quote(_required_placeholder('SURFSENSE_ZERO_UPSTREAM_MAX_CONNS'))}\n"
        f"      ZERO_CVR_MAX_CONNS: {_quote(_required_placeholder('SURFSENSE_ZERO_CVR_MAX_CONNS'))}\n"
        f"      ZERO_QUERY_URL: {_quote(_required_placeholder('SURFSENSE_ZERO_QUERY_URL'))}\n"
        f"      ZERO_MUTATE_URL: {_quote(_required_placeholder('SURFSENSE_ZERO_MUTATE_URL'))}\n"
        "    entrypoint: [\"sh\", \"-c\"]\n"
        "    command:\n"
        "      - 'if [ -f /zero-init/needs_reset ]; then echo \"[zero-init] publication change detected; wiping replica file(s) under /data\" && rm -f /data/zero.db /data/zero.db-shm /data/zero.db-wal && rm -f /zero-init/needs_reset; fi; exec zero-cache'\n"
        "    labels:\n"
        f"{_traefik_labels('zero-cache', 'SURFSENSE_ZERO_HOSTNAME', 4848)}"
        "    depends_on:\n"
        "      migrations:\n"
        "        condition: service_completed_successfully\n"
        "    healthcheck:\n"
        "      test: [\"CMD\", \"curl\", \"-f\", \"http://localhost:4848/keepalive\"]\n"
        "      interval: 10s\n"
        "      timeout: 5s\n"
        "      retries: 5\n"
        "    volumes:\n"
        "      - surfsense-zero-cache:/data\n"
        "      - surfsense-zero-init:/zero-init\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
    )


def _frontend_service(*, web_image: str) -> str:
    return (
        "  frontend:\n"
        f"    image: {web_image}\n"
        "    restart: unless-stopped\n"
        "    expose:\n"
        "      - '3000'\n"
        "    environment:\n"
        f"      NEXT_PUBLIC_FASTAPI_BACKEND_URL: {_quote(_required_placeholder('SURFSENSE_API_URL'))}\n"
        f"      NEXT_PUBLIC_ZERO_CACHE_URL: {_quote(_required_placeholder('SURFSENSE_ZERO_URL'))}\n"
        f"      NEXT_PUBLIC_FASTAPI_BACKEND_AUTH_TYPE: {_quote(_required_placeholder('SURFSENSE_AUTH_TYPE'))}\n"
        f"      NEXT_PUBLIC_ETL_SERVICE: {_quote(_required_placeholder('SURFSENSE_ETL_SERVICE'))}\n"
        f"      NEXT_PUBLIC_DEPLOYMENT_MODE: {_quote(_required_placeholder('SURFSENSE_FRONTEND_DEPLOYMENT_MODE'))}\n"
        f"      FASTAPI_BACKEND_INTERNAL_URL: {_quote(_required_placeholder('SURFSENSE_FASTAPI_BACKEND_INTERNAL_URL'))}\n"
        "    labels:\n"
        f"{_traefik_labels('frontend', 'SURFSENSE_FRONTEND_HOSTNAME', 3000)}"
        "    depends_on:\n"
        "      backend:\n"
        "        condition: service_healthy\n"
        "      zero-cache:\n"
        "        condition: service_healthy\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
    )


def _backend_common_env(*, service_name: str) -> str:
    service_role_env_name = _SERVICE_ROLE_ENV_NAMES[service_name]
    return (
        f"      DATABASE_URL: {_quote(_required_placeholder('SURFSENSE_DATABASE_URL'))}\n"
        f"      DB_PASSWORD: {_quote(_required_placeholder('SURFSENSE_DB_PASSWORD'))}\n"
        f"      CELERY_BROKER_URL: {_quote(_required_placeholder('SURFSENSE_REDIS_URL'))}\n"
        f"      CELERY_RESULT_BACKEND: {_quote(_required_placeholder('SURFSENSE_REDIS_URL'))}\n"
        f"      REDIS_APP_URL: {_quote(_required_placeholder('SURFSENSE_REDIS_URL'))}\n"
        f"      SECRET_KEY: {_quote(_required_placeholder('SURFSENSE_SECRET_KEY'))}\n"
        f"      JWT_SECRET: {_quote(_required_placeholder('SURFSENSE_JWT_SECRET'))}\n"
        f"      LITELLM_API_KEY: {_quote(_required_placeholder('SURFSENSE_LITELLM_VIRTUAL_KEY'))}\n"
        f"      OPENAI_API_KEY: {_quote(_required_placeholder('SURFSENSE_LITELLM_VIRTUAL_KEY'))}\n"
        f"      LITELLM_API_BASE: {_quote(_required_placeholder('SURFSENSE_LITELLM_BASE_URL'))}\n"
        f"      OPENAI_API_BASE: {_quote(_required_placeholder('SURFSENSE_LITELLM_BASE_URL'))}\n"
        f"      EMBEDDING_MODEL: {_quote(_required_placeholder('SURFSENSE_EMBEDDING_MODEL'))}\n"
        f"      AUTH_TYPE: {_quote(_required_placeholder('SURFSENSE_AUTH_TYPE'))}\n"
        f"      ETL_SERVICE: {_quote(_required_placeholder('SURFSENSE_ETL_SERVICE'))}\n"
        f"      CELERY_TASK_DEFAULT_QUEUE: {_quote(_required_placeholder('SURFSENSE_CELERY_TASK_DEFAULT_QUEUE'))}\n"
        f"      PYTHONPATH: {_quote(_required_placeholder('SURFSENSE_PYTHONPATH'))}\n"
        f"      SERVICE_ROLE: {_quote(_required_placeholder(service_role_env_name))}\n"
    )


def _traefik_labels(service_name: str, hostname_env_name: str, port: int) -> str:
    router_name = f"surfsense-{service_name}".replace("_", "-")
    hostname_placeholder = _required_placeholder(hostname_env_name)
    return (
        "      traefik.enable: \"true\"\n"
        f"      traefik.http.routers.{router_name}.entrypoints: \"websecure\"\n"
        f"      traefik.http.routers.{router_name}.rule: \"Host(`{hostname_placeholder}`)\"\n"
        f"      traefik.http.routers.{router_name}.tls: \"true\"\n"
        f"      traefik.http.services.{router_name}.loadbalancer.server.port: \"{port}\"\n"
    )


def _global_llm_config(stack_name: str, litellm_model_aliases: tuple[str, ...]) -> str:
    del stack_name
    content = (
        "router_settings:\n"
        "  routing_strategy: usage-based-routing\n"
        "  num_retries: 3\n"
        "  allowed_fails: 3\n"
        "  cooldown_time: 60\n"
        "global_llm_configs:\n"
    )
    for index, alias in enumerate(litellm_model_aliases, start=1):
        model_name = "${SURFSENSE_LITELLM_MODEL:?SURFSENSE_LITELLM_MODEL is required}" if index == 1 else alias
        entry_name = f"LiteLLM - {alias}"
        content += (
            f"  - id: -{index}\n"
            f"    name: {_yaml_double_quoted(entry_name)}\n"
            "    description: Wizard-managed SurfSense gateway through shared LiteLLM\n"
            "    billing_tier: free\n"
            "    anonymous_enabled: false\n"
            "    seo_enabled: false\n"
            "    provider: OPENAI\n"
            f"    model_name: {model_name}\n"
            "    api_key: ${SURFSENSE_LITELLM_VIRTUAL_KEY:?SURFSENSE_LITELLM_VIRTUAL_KEY is required}\n"
            "    api_base: ${SURFSENSE_LITELLM_BASE_URL:?SURFSENSE_LITELLM_BASE_URL is required}/v1\n"
            "    rpm: 200\n"
            "    tpm: 1000000\n"
            "    litellm_params:\n"
            "      temperature: 0.7\n"
            "      max_tokens: 4000\n"
            "    system_instructions: \"\"\n"
            "    use_default_system_instructions: true\n"
            "    citations_enabled: true\n"
        )
    return "  surfsense-global-llm-config:\n    content: |\n" + _indent(content, 6)


def _surfsense_litellm_model_aliases(
    *,
    primary_model: str,
    models: tuple[str, ...] | None,
) -> tuple[str, ...]:
    aliases = [primary_model, *(models or ())]
    normalized = tuple(alias.strip() for alias in aliases if alias.strip())
    return tuple(dict.fromkeys(normalized)) or (_DEFAULT_LITELLM_MODEL,)


def _yaml_double_quoted(value: str) -> str:
    return json.dumps(value)


def _surfsense_secrets(generated_secrets: SurfSenseGeneratedSecrets | None) -> dict[str, str]:
    if generated_secrets is not None:
        return dict(generated_secrets.secrets)
    return {
        "db_password": _generated_secret("surfsense-db-password"),
        "jwt_secret": _generated_secret("surfsense-jwt-secret"),
        "searxng_secret": _generated_secret("surfsense-searxng-secret"),
        "secret_key": _generated_secret("surfsense-secret-key"),
        "zero_admin_password": _generated_secret("surfsense-zero-admin-password"),
    }


def _database_url(
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    password: str,
) -> str:
    return (
        f"postgresql+asyncpg://{postgres.user_name}:{password}"
        f"@{postgres_service_name}:5432/{postgres.database_name}"
    )


def _zero_database_url(
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    password: str,
) -> str:
    return (
        f"postgresql://{postgres.user_name}:{password}"
        f"@{postgres_service_name}:5432/{postgres.database_name}?sslmode=disable"
    )


def _redis_url(redis_service_name: str) -> str:
    password = _generated_secret(f"{redis_service_name}-password")
    return f"redis://:{password}@{redis_service_name}:6379/0"


def _litellm_internal_base_url(stack_name: str) -> str:
    return f"http://{stack_name}-shared-litellm:4000"


def _litellm_virtual_key_value(generated_keys: LiteLLMGeneratedKeys | None) -> str:
    if generated_keys is not None and "surfsense" in generated_keys.virtual_keys:
        return generated_keys.virtual_keys["surfsense"]
    return _generated_secret("litellm-virtual-key-surfsense")


def _surfsense_backend_image(version: str) -> str:
    return f"ghcr.io/modsetter/surfsense-backend:{_image_tag(version)}"


def _surfsense_web_image(version: str) -> str:
    return f"ghcr.io/modsetter/surfsense-web:{_image_tag(version)}"


def _image_tag(version: str) -> str:
    stripped = version.strip()
    return stripped or _DEFAULT_SURFSENSE_VERSION


def _non_empty_or_default(value: str | None, default: str) -> str:
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _runtime_env_spec(
    *,
    name: str,
    value: str,
    target_services: tuple[str, ...],
) -> DokployEnvSpec:
    return _env_spec(
        name=name,
        value=value,
        target_services=target_services,
        source="surfsense-runtime-config",
        sensitive=False,
    )


def _env_spec(
    *,
    name: str,
    value: str,
    target_services: tuple[str, ...],
    source: str,
    sensitive: bool = True,
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(
            name=name,
            value=value,
            sensitive=sensitive,
            source=source,
        ),
        owner="surfsense",
        target_services=target_services,
        placeholder=_required_placeholder(name),
        required=True,
    )


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _quote(value: str) -> str:
    return json.dumps(value)


def _indent(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "".join(f"{prefix}{line}\n" for line in value.splitlines())


def _generated_secret(secret_ref: str) -> str:
    return "dw-" + sha256(secret_ref.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "SurfSenseBootstrapError",
    "SurfSenseBootstrapResult",
    "SurfSenseHttpClient",
    "SurfSenseHttpResponse",
    "SurfSenseReadinessError",
    "ensure_surfsense_first_user_bootstrap",
    "render_surfsense_compose_for_state",
    "_render_compose_file",
]
