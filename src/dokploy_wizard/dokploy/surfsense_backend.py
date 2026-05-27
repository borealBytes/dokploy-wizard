# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed SurfSense runtime backend."""

from __future__ import annotations

import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib import error, parse, request

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    apply_rendered_compose_to_existing,
    persist_compose_artifact_hash_if_checkpoint_present,
)
from dokploy_wizard.dokploy.surfsense import (
    SurfSenseReadinessError,
    ensure_surfsense_first_user_bootstrap,
    render_surfsense_compose_for_state,
)
from dokploy_wizard.packs.surfsense import (
    SurfSenseBootstrapState,
    SurfSenseError,
    SurfSenseResourceRecord,
)
from dokploy_wizard.verification import make_verification_result

_SURFSENSE_BOOTSTRAP_READINESS_ATTEMPTS = 120
_SURFSENSE_BOOTSTRAP_READINESS_DELAY_SECONDS = 5.0
_SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS = 360
_SURFSENSE_MIGRATIONS_WAIT_DELAY_SECONDS = 5.0


class DokploySurfSenseApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...

    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokploySurfSenseBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        state_dir: Path,
        stack_name: str,
        frontend_hostname: str,
        api_hostname: str,
        zero_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        admin_email: str,
        admin_password: str,
        litellm_model: str | None = None,
        litellm_models: tuple[str, ...] | None = None,
        surfsense_version: str = "0.0.25",
        frontend_public_url: str | None = None,
        api_public_url: str | None = None,
        zero_public_url: str | None = None,
        auth_type: str = "LOCAL",
        etl_service: str = "DOCLING",
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        client: DokploySurfSenseApi | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._frontend_hostname = frontend_hostname
        self._api_hostname = api_hostname
        self._zero_hostname = zero_hostname
        self._postgres_service_name = postgres_service_name
        self._redis_service_name = redis_service_name
        self._postgres = postgres
        self._redis = redis
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._litellm_model = litellm_model
        self._litellm_models = litellm_models
        self._surfsense_version = surfsense_version
        self._frontend_public_url = frontend_public_url
        self._api_public_url = api_public_url
        self._zero_public_url = zero_public_url
        self._auth_type = auth_type
        self._etl_service = etl_service
        self._embedding_model = embedding_model
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    def get_service(self, resource_id: str) -> SurfSenseResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return SurfSenseResourceRecord(resource_id=resource_id, resource_name=_service_name(self._stack_name))

    def find_service_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if resource_name != _service_name(self._stack_name) or self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SurfSenseResourceRecord(resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name)

    def create_service(self, **kwargs: object) -> SurfSenseResourceRecord:
        resource_name = str(kwargs["resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise SurfSenseError("SurfSense service name does not match the active Dokploy plan.")
        self._validate_inputs(kwargs)
        locator = self._ensure_compose_applied()
        return SurfSenseResourceRecord(resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name)

    def update_service(self, **kwargs: object) -> SurfSenseResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> SurfSenseResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return SurfSenseResourceRecord(resource_id=resource_id, resource_name=_data_name(self._stack_name))

    def find_persistent_data_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if resource_name != _data_name(self._stack_name) or self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return SurfSenseResourceRecord(resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name)

    def create_persistent_data(self, resource_name: str) -> SurfSenseResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise SurfSenseError("SurfSense data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return SurfSenseResourceRecord(resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name)

    def check_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service
        return _local_https_health_check(url)

    def check_internal_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service
        return _docker_exec_internal_health_check(stack_name=self._stack_name, url=url)

    def ensure_application_ready(self) -> tuple[SurfSenseBootstrapState, tuple[str, ...]]:
        diagnostic_secrets = (
            self._admin_password,
            self._postgres.user_name,
            self._postgres.database_name,
        )
        _wait_for_surfsense_migrations_terminal(
            stack_name=self._stack_name,
            secrets=diagnostic_secrets,
            attempts=_SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS,
            delay_seconds=_SURFSENSE_MIGRATIONS_WAIT_DELAY_SECONDS,
        )
        try:
            result = ensure_surfsense_first_user_bootstrap(
                api_hostname=self._api_hostname,
                frontend_hostname=self._frontend_hostname,
                admin_email=self._admin_email,
                admin_password=self._admin_password,
                readiness_attempts=_SURFSENSE_BOOTSTRAP_READINESS_ATTEMPTS,
                readiness_delay_seconds=_SURFSENSE_BOOTSTRAP_READINESS_DELAY_SECONDS,
            )
        except SurfSenseReadinessError as error:
            migration_detail = _surfsense_migrations_failure_detail(
                stack_name=self._stack_name,
                secrets=diagnostic_secrets,
            )
            if migration_detail is None:
                raise
            raise SurfSenseError(f"{error} {migration_detail}") from error
        return (
            SurfSenseBootstrapState(
                created=result.created,
                verified_existing=result.verified_existing,
            ),
            result.notes,
        )

    def _validate_inputs(self, kwargs: dict[str, object]) -> None:
        if str(kwargs["frontend_hostname"]) != self._frontend_hostname:
            raise SurfSenseError("SurfSense frontend hostname no longer matches the active Dokploy plan.")
        if str(kwargs["api_hostname"]) != self._api_hostname:
            raise SurfSenseError("SurfSense API hostname no longer matches the active Dokploy plan.")
        if str(kwargs["zero_hostname"]) != self._zero_hostname:
            raise SurfSenseError("SurfSense Zero hostname no longer matches the active Dokploy plan.")
        if str(kwargs["postgres_service_name"]) != self._postgres_service_name:
            raise SurfSenseError("SurfSense postgres service no longer matches the active Dokploy plan.")
        if str(kwargs["redis_service_name"]) != self._redis_service_name:
            raise SurfSenseError("SurfSense Redis service no longer matches the active Dokploy plan.")
        if kwargs["postgres"] != self._postgres or kwargs["redis"] != self._redis:
            raise SurfSenseError("SurfSense shared-core bindings no longer match the active Dokploy plan.")
        if str(kwargs["data_resource_name"]) != _data_name(self._stack_name):
            raise SurfSenseError("SurfSense data resource name no longer matches the active Dokploy plan.")

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise SurfSenseError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._compose_name:
                    locator = _ComposeLocator(project.project_id, environment.environment_id, compose.compose_id)
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        compose_file = render_surfsense_compose_for_state(
            stack_name=self._stack_name,
            frontend_hostname=self._frontend_hostname,
            api_hostname=self._api_hostname,
            zero_hostname=self._zero_hostname,
            postgres_service_name=self._postgres_service_name,
            redis_service_name=self._redis_service_name,
            postgres=self._postgres,
            redis=self._redis,
            state_dir=self._state_dir,
            litellm_model=self._litellm_model,
            litellm_models=self._litellm_models,
            surfsense_version=self._surfsense_version,
            frontend_public_url=self._frontend_public_url,
            api_public_url=self._api_public_url,
            zero_public_url=self._zero_public_url,
            auth_type=self._auth_type,
            etl_service=self._etl_service,
            embedding_model=self._embedding_model,
        )
        try:
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._compose_name:
                        locator = _ComposeLocator(project.project_id, environment.environment_id, compose.compose_id)
                        applied = apply_compose_noop_guard(
                            rendered_compose=compose_file,
                            service_key=self._compose_name,
                            state_dir=self._state_dir,
                            client=self._client,
                            locator=locator,
                            compose_id=compose.compose_id,
                            title="dokploy-wizard surfsense reconcile",
                            description="Update SurfSense compose app",
                            verify_current=self._verify_current_service,
                            locator_factory=lambda compose_id: _ComposeLocator(project.project_id, environment.environment_id, compose_id),
                        )
                        self._created_in_process = True
                        self._applied_locator = applied.locator
                        return applied.locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file="services: {}\n",
                    app_name=self._compose_name,
                )
                apply_rendered_compose_to_existing(client=self._client, compose_id=created.compose_id, rendered_compose=compose_file)
                _deploy_compose_or_raise(
                    client=self._client,
                    compose_id=created.compose_id,
                    service_key=self._compose_name,
                    title="dokploy-wizard surfsense reconcile",
                    description="Create SurfSense compose app",
                )
                persist_compose_artifact_hash_if_checkpoint_present(state_dir=self._state_dir, service_key=self._compose_name, rendered_compose=compose_file)
                locator = _ComposeLocator(project.project_id, environment.environment_id, created.compose_id)
                self._created_in_process = True
                self._applied_locator = locator
                return locator
            created_project = self._client.create_project(name=self._stack_name, description="Managed by dokploy-wizard", env=None)
            created_compose = self._client.create_compose(name=self._compose_name, environment_id=created_project.environment_id, compose_file="services: {}\n", app_name=self._compose_name)
            apply_rendered_compose_to_existing(client=self._client, compose_id=created_compose.compose_id, rendered_compose=compose_file)
            _deploy_compose_or_raise(
                client=self._client,
                compose_id=created_compose.compose_id,
                service_key=self._compose_name,
                title="dokploy-wizard surfsense reconcile",
                description="Create SurfSense compose app",
            )
            persist_compose_artifact_hash_if_checkpoint_present(state_dir=self._state_dir, service_key=self._compose_name, rendered_compose=compose_file)
        except DokployApiError as error:
            raise SurfSenseError(str(error)) from error
        locator = _ComposeLocator(created_project.project_id, created_project.environment_id, created_compose.compose_id)
        self._created_in_process = True
        self._applied_locator = locator
        return locator

    def _verify_current_service(self):
        checks = {
            "public frontend app": _local_https_health_check(f"https://{self._frontend_hostname}/"),
            "public backend /ready": _local_https_health_check(f"https://{self._api_hostname}/ready"),
            "public zero-cache /keepalive": _local_https_health_check(f"https://{self._zero_hostname}/keepalive"),
            "internal SearXNG /healthz": _docker_exec_internal_health_check(
                stack_name=self._stack_name,
                url="http://searxng:8080/healthz",
            ),
        }
        failed = [label for label, passed in checks.items() if not passed]
        return make_verification_result(
            service_name=self._compose_name,
            tier="app",
            passed=not failed,
            detail=(
                f"SurfSense runtime for '{self._compose_name}' passed public and internal health checks."
                if not failed
                else f"SurfSense runtime for '{self._compose_name}' failed checks: {', '.join(failed)}."
            ),
            evidence_command=["python3", "-m", "dokploy_wizard.service_verification_runner"],
        )


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-surfsense"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-surfsense-data"


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:surfsense-{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":surfsense-{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _deploy_compose_or_raise(
    *,
    client: DokploySurfSenseApi,
    compose_id: str,
    service_key: str,
    title: str,
    description: str,
) -> None:
    # Dokploy reports whether the deploy request was accepted, not whether image
    # extraction and dependency-gated containers have finished. Docker polling is
    # still the proof point before first-user bootstrap.
    deployment = client.deploy_compose(
        compose_id=compose_id,
        title=title,
        description=description,
    )
    if not deployment.success:
        detail = f": {deployment.message}" if deployment.message else ""
        raise SurfSenseError(
            f"Dokploy deploy for compose service '{service_key}' did not report success{detail}."
        )


def _local_https_health_check(url: str) -> bool:
    parsed_url = parse.urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.hostname is None:
        return False
    status = _local_https_status(parsed_url.netloc, parsed_url.path or "/")
    if status == 200:
        return True
    if parsed_url.path == "/ready" and status == 404:
        return _local_https_status(parsed_url.netloc, "/auth/register") == 405
    return False


def _local_https_status(host: str, path: str) -> int | None:
    req = request.Request(
        f"https://127.0.0.1{path}",
        headers={"Host": host},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=20, context=ssl._create_unverified_context()) as response:  # noqa: S310
            response.read()
            return response.status
    except error.HTTPError as exc:
        exc.read()
        return exc.code
    except (error.URLError, OSError, TimeoutError):
        return None


def _docker_exec_internal_health_check(*, stack_name: str, url: str) -> bool:
    if not url.startswith("http://"):
        return False
    backend_container = _find_surfsense_backend_container(stack_name)
    if backend_container is None:
        return False
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                backend_container,
                "python3",
                "-c",
                (
                    "import sys, urllib.request; "
                    "response = urllib.request.urlopen(sys.argv[1], timeout=10); "
                    "response.read(); "
                    "sys.exit(0 if response.status == 200 else 1)"
                ),
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    return completed.returncode == 0


def _surfsense_migrations_failure_detail(
    *, stack_name: str, secrets: tuple[str, ...] = ()
) -> str | None:
    migration_container = _find_surfsense_container(stack_name, service_token="migrations")
    if migration_container is None:
        return None
    if not _surfsense_migrations_status_failed(migration_container[2]):
        return None
    return _surfsense_migrations_failure_detail_for_container(
        migration_container,
        secrets=secrets,
    )


def _wait_for_surfsense_migrations_terminal(
    *,
    stack_name: str,
    secrets: tuple[str, ...] = (),
    attempts: int = _SURFSENSE_MIGRATIONS_WAIT_ATTEMPTS,
    delay_seconds: float = _SURFSENSE_MIGRATIONS_WAIT_DELAY_SECONDS,
) -> None:
    last_state = "migrations container has not been created yet"
    saw_migration_container = False
    for attempt in range(attempts):
        migration_container = _find_surfsense_container(stack_name, service_token="migrations")
        if migration_container is None:
            last_state = "migrations container has not been created yet"
        else:
            saw_migration_container = True
            _, name, status = migration_container
            last_state = f"container={name}, status={status}"
            if _surfsense_migrations_status_succeeded(status):
                return
            if _surfsense_migrations_status_failed(status):
                raise SurfSenseError(
                    _surfsense_migrations_failure_detail_for_container(
                        migration_container,
                        secrets=secrets,
                    )
                )
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    detail = f"Last migration state: {last_state}."
    if not saw_migration_container:
        detail += (
            " Dokploy deployment may still be pulling images or creating containers; "
            "compose.deploy can return before the compose rollout has finished."
        )
    raise SurfSenseError(
        "SurfSense migrations container did not reach a terminal state before "
        f"first-user bootstrap after {attempts * delay_seconds:.0f}s. {detail}"
    )


def _surfsense_migrations_status_succeeded(status: str) -> bool:
    return "exited (0)" in status.lower()


def _surfsense_migrations_status_failed(status: str) -> bool:
    normalized = status.lower()
    if "exited (0)" in normalized:
        return False
    if "exited (" in normalized:
        return True
    return "error" in normalized or "dead" in normalized


def _surfsense_migrations_failure_detail_for_container(
    migration_container: tuple[str, str, str], *, secrets: tuple[str, ...]
) -> str:
    container_id, name, status = migration_container
    try:
        completed = subprocess.run(
            ["docker", "logs", "--tail", "120", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        logs = "<unable to read docker logs>"
    else:
        logs = (completed.stdout + completed.stderr).strip() or "<no docker logs>"
    return (
        "SurfSense migrations container failed before backend startup "
        f"(container={name}, status={status}). Recent logs: "
        f"{_redact_surfsense_diagnostic_text(logs, secrets)}"
    )


def _find_surfsense_backend_container(stack_name: str) -> str | None:
    container = _find_surfsense_container(stack_name, service_token="backend", running_only=True)
    return None if container is None else container[0]


def _find_surfsense_container(
    stack_name: str, *, service_token: str, running_only: bool = False
) -> tuple[str, str, str] | None:
    try:
        completed = subprocess.run(
            [
                "docker",
                "ps" if running_only else "ps",
                *([] if running_only else ["-a"]),
                "--format",
                "{{.ID}}\t{{.Names}}\t{{.Status}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if completed.returncode != 0:
        return None
    stack_token = stack_name.replace("-", "").lower()
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        container_id, name, status = parts
        normalized = name.replace("-", "").replace("_", "").lower()
        if (
            stack_token in normalized
            and "surfsense" in normalized
            and service_token.replace("-", "").replace("_", "").lower() in normalized
        ):
            return container_id, name, status
    return None


def _redact_surfsense_diagnostic_text(text: str, secrets: tuple[str, ...]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<REDACTED>")
    return redacted
