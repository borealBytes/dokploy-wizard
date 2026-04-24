"""Multica runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
import os
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import PackSharedAllocation, SharedPostgresAllocation
from dokploy_wizard.packs.multica.models import (
    MulticaBootstrapState,
    MulticaHealthState,
    MulticaResourceRecord,
    MulticaResult,
    MulticaServiceConfig,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

MULTICA_SERVICE_RESOURCE_TYPE = "multica_service"
MULTICA_DATA_RESOURCE_TYPE = "multica_data"


class MulticaError(RuntimeError):
    """Raised when Multica reconciliation fails or detects drift."""


class MulticaBackend(Protocol):
    def get_service(self, resource_id: str) -> MulticaResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> MulticaResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        api_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> MulticaResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        api_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> MulticaResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> MulticaResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> MulticaResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> MulticaResourceRecord: ...

    def check_health(self, *, service: MulticaResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(
        self,
        *,
        jwt_secret_ref: str,
        frontend_origin: str,
    ) -> MulticaBootstrapState: ...


class ShellMulticaBackend:
    def __init__(self) -> None:
        self._service: MulticaResourceRecord | None = None
        self._data: MulticaResourceRecord | None = None

    def get_service(self, resource_id: str) -> MulticaResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return MulticaResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> MulticaResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        api_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> MulticaResourceRecord:
        del hostname, api_hostname, postgres_service_name, postgres, data_resource_name, env
        self._service = MulticaResourceRecord(
            resource_id=resource_name,
            resource_name=resource_name,
        )
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        api_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> MulticaResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            api_hostname=api_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
            env=env,
        )

    def get_persistent_data(self, resource_id: str) -> MulticaResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return MulticaResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> MulticaResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> MulticaResourceRecord:
        self._data = MulticaResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: MulticaResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(
        self,
        *,
        jwt_secret_ref: str,
        frontend_origin: str,
    ) -> MulticaBootstrapState:
        del jwt_secret_ref, frontend_origin
        return MulticaBootstrapState(
            ready=True,
            phases_complete=("env", "health"),
        )


def reconcile_multica(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: MulticaBackend,
) -> MulticaResult:
    if "multica" not in desired_state.enabled_packs:
        return MulticaResult(
            outcome="skipped",
            enabled=False,
            hostname=None,
            api_hostname=None,
            service=None,
            data=None,
            health=None,
            bootstrap=None,
            config=None,
            notes=("Multica pack is explicitly disabled for this install.",),
        )

    hostname = desired_state.hostnames.get("multica")
    if hostname is None:
        raise MulticaError(
            "Desired state is missing the canonical Multica hostname at hostnames['multica']."
        )
    api_hostname = (
        desired_state.machine_hostnames.get("multica-api")
        or desired_state.hostnames.get("multica-api")
    )
    if api_hostname is None:
        raise MulticaError(
            "Desired state is missing the machine Multica API hostname at "
            "machine_hostnames['multica-api']."
        )

    _, postgres_service_name, postgres = _get_multica_allocation(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    frontend_origin = f"https://{hostname}"
    health_url = f"https://{api_hostname}/health"
    jwt_secret_ref = _jwt_secret_ref(desired_state.stack_name)
    env = _build_env_contract(
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        jwt_secret_ref=jwt_secret_ref,
        frontend_origin=frontend_origin,
    )

    data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=MULTICA_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=MULTICA_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
    )
    service, service_id = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        api_hostname=api_hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_name=data_name,
        env=env,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    config = MulticaServiceConfig(
        hostname=hostname,
        api_hostname=api_hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
    )

    if dry_run:
        return MulticaResult(
            outcome="plan_only",
            enabled=True,
            hostname=hostname,
            api_hostname=api_hostname,
            service=service,
            data=data,
            health=MulticaHealthState(
                healthy=False,
                checks=(f"pending:{health_url}",),
            ),
            bootstrap=MulticaBootstrapState(ready=False, phases_complete=()),
            config=config,
            notes=(
                f"Multica service '{service_name}' will be exposed at '{hostname}'.",
                f"Multica API health is verified via '{health_url}'.",
                "Multica requires DATABASE_URL, JWT_SECRET, and FRONTEND_ORIGIN in runtime env.",
            ),
        )

    initial_health = backend.check_health(service=service, url=health_url)
    bootstrap = backend.ensure_application_ready(
        jwt_secret_ref=jwt_secret_ref,
        frontend_origin=frontend_origin,
    )
    final_health = initial_health or backend.check_health(service=service, url=health_url)
    if not final_health:
        raise MulticaError(f"Multica health check failed for '{health_url}'.")

    return MulticaResult(
        outcome="applied",
        enabled=True,
        hostname=hostname,
        api_hostname=api_hostname,
        service=service,
        data=data,
        health=MulticaHealthState(
            healthy=True,
            checks=(f"passed:{health_url}",),
        ),
        bootstrap=bootstrap,
        config=config,
        notes=(
            f"Multica service '{service_name}' is reconciled and healthy.",
            f"Multica data persists in '{data_name}'.",
        ),
    )


def _get_multica_allocation(
    desired_state: DesiredState,
) -> tuple[PackSharedAllocation, str, SharedPostgresAllocation]:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "multica"
    ]
    if (
        len(matches) != 1
        or matches[0].postgres is None
        or desired_state.shared_core.postgres is None
    ):
        raise MulticaError("Multica shared-core postgres allocation is missing from desired state.")
    return matches[0], desired_state.shared_core.postgres.service_name, matches[0].postgres


def _build_env_contract(
    *,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    jwt_secret_ref: str,
    frontend_origin: str,
    allowed_email_domains: str | None = None,
    allowed_emails: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "DATABASE_URL": (
            f"postgres://{postgres.user_name}:change-me"
            f"@{postgres_service_name}:5432/{postgres.database_name}?sslmode=disable"
        ),
        "JWT_SECRET": "change-me",
        "FRONTEND_ORIGIN": frontend_origin,
        "ALLOW_SIGNUP": "false",
    }
    if allowed_email_domains is None:
        allowed_email_domains = os.getenv("ALLOWED_EMAIL_DOMAINS")
    if allowed_email_domains:
        env["ALLOWED_EMAIL_DOMAINS"] = allowed_email_domains
    if allowed_emails is None:
        allowed_emails = os.getenv("ALLOWED_EMAILS")
    if allowed_emails:
        env["ALLOWED_EMAILS"] = allowed_emails
    return env


def _secret_placeholder(secret_ref: str) -> str:
    return f"secretref:{secret_ref}"


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], MulticaResourceRecord | None],
    find_by_name: Callable[[str], MulticaResourceRecord | None],
    create_resource: Callable[[str], MulticaResourceRecord],
) -> tuple[MulticaResourceRecord, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise MulticaError(
                f"Ownership ledger says the Multica {resource_type} exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise MulticaError(
                f"Ownership ledger Multica {resource_type} no longer matches "
                "the desired naming convention."
            )
        return existing, existing.resource_id

    existing = find_by_name(resource_name)
    if existing is not None:
        if dry_run:
            return existing, existing.resource_id
        created = create_resource(resource_name)
        return created, created.resource_id

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            MulticaResourceRecord(
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return created, created.resource_id


def _resolve_service(
    *,
    dry_run: bool,
    service_name: str,
    hostname: str,
    api_hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    data_name: str,
    env: dict[str, str],
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: MulticaBackend,
) -> tuple[MulticaResourceRecord, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=MULTICA_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise MulticaError(
                "Ownership ledger says the Multica service exists, but the "
                "backend could not find it."
            )
        if existing.resource_name != service_name:
            raise MulticaError(
                "Ownership ledger Multica service no longer matches the desired naming convention."
            )
        if dry_run:
            return existing, existing.resource_id
        updated = backend.update_service(
            resource_id=existing.resource_id,
            resource_name=service_name,
            hostname=hostname,
            api_hostname=api_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
            env=env,
        )
        return updated, updated.resource_id

    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if dry_run:
            return existing, existing.resource_id
        created = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            api_hostname=api_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
            env=env,
        )
        return created, created.resource_id

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return MulticaResourceRecord(resource_id=planned_id, resource_name=service_name), planned_id

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        api_hostname=api_hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_resource_name=data_name,
        env=env,
    )
    return created, created.resource_id


def _find_owned_resource(
    *, ownership_ledger: OwnershipLedger, resource_type: str, scope: str
) -> OwnedResource | None:
    return next(
        (
            resource
            for resource in ownership_ledger.resources
            if resource.resource_type == resource_type and resource.scope == scope
        ),
        None,
    )


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-multica"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-multica-data"


def _jwt_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-multica-jwt-secret"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:multica:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:multica:data"


def _http_health_check(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection("127.0.0.1", 443, timeout=10)
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={"Host": parsed.netloc},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()
