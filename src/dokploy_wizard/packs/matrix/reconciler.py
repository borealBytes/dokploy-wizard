"""Matrix runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol

from dokploy_wizard.core.models import PackSharedAllocation
from dokploy_wizard.packs.matrix.models import (
    MatrixHealthCheck,
    MatrixManagedResource,
    MatrixPhase,
    MatrixResourceRecord,
    MatrixResult,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput

MATRIX_SERVICE_RESOURCE_TYPE = "matrix_service"
MATRIX_DATA_RESOURCE_TYPE = "matrix_data"


class MatrixError(RuntimeError):
    """Raised when Matrix reconciliation fails or detects drift."""


class MatrixBackend(Protocol):
    def get_service(self, resource_id: str) -> MatrixResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> MatrixResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
        shared_allocation: PackSharedAllocation,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> MatrixResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> MatrixResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> MatrixResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> MatrixResourceRecord: ...

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool: ...


class ShellMatrixBackend:
    """Deterministic default backend for Matrix runtime planning and health checks."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_existing_service_id = values.get("MATRIX_MOCK_EXISTING_SERVICE_ID")
        self._forced_existing_data_id = values.get("MATRIX_MOCK_EXISTING_DATA_ID")
        self._forced_health = _optional_bool(values, "MATRIX_MOCK_HEALTHY")
        self._service: MatrixResourceRecord | None = None
        self._data: MatrixResourceRecord | None = None

    def get_service(self, resource_id: str) -> MatrixResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        if self._forced_existing_service_id == resource_id:
            return MatrixResourceRecord(resource_id=resource_id, resource_name=resource_id)
        return MatrixResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        if self._forced_existing_service_id is None:
            return None
        return MatrixResourceRecord(
            resource_id=self._forced_existing_service_id,
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
        shared_allocation: PackSharedAllocation,
        postgres_service_name: str,
        redis_service_name: str,
        data_resource_name: str,
    ) -> MatrixResourceRecord:
        del (
            hostname,
            secret_refs,
            shared_allocation,
            postgres_service_name,
            redis_service_name,
            data_resource_name,
        )
        self._service = MatrixResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._service

    def get_persistent_data(self, resource_id: str) -> MatrixResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        if self._forced_existing_data_id == resource_id:
            return MatrixResourceRecord(resource_id=resource_id, resource_name=resource_id)
        return MatrixResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> MatrixResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        if self._forced_existing_data_id is None:
            return None
        return MatrixResourceRecord(
            resource_id=self._forced_existing_data_id,
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> MatrixResourceRecord:
        self._data = MatrixResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: MatrixResourceRecord, url: str) -> bool:
        del service
        if self._forced_health is not None:
            return self._forced_health
        return _http_health_check(url)


def reconcile_matrix(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: MatrixBackend,
) -> MatrixPhase:
    if "matrix" not in desired_state.enabled_packs:
        return MatrixPhase(
            result=MatrixResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                shared_postgres_service=None,
                shared_redis_service=None,
                shared_allocation=None,
                secret_refs=(),
                health_check=None,
                notes=("Matrix pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("matrix")
    if hostname is None:
        raise MatrixError(
            "Desired state is missing the canonical Matrix hostname at hostnames['matrix']."
        )

    postgres_service = desired_state.shared_core.postgres
    redis_service = desired_state.shared_core.redis
    matrix_allocation = _get_matrix_allocation(desired_state)
    if postgres_service is None or redis_service is None or matrix_allocation is None:
        raise MatrixError(
            "Matrix requires shared-core postgres, shared-core redis, and a matrix allocation "
            "in desired_state.shared_core."
        )

    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    secret_refs = _secret_refs(desired_state.stack_name)
    health_url = f"https://{hostname}/_matrix/client/versions"

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=MATRIX_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=MATRIX_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
        collision_message=(
            "Existing Matrix persistent data matched the desired name but is not wizard-owned."
        ),
    )
    service, service_id = _resolve_matrix_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        secret_refs=secret_refs,
        shared_allocation=matrix_allocation,
        postgres_service_name=postgres_service.service_name,
        redis_service_name=redis_service.service_name,
        data_resource_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    if dry_run:
        return MatrixPhase(
            result=MatrixResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                shared_postgres_service=postgres_service.service_name,
                shared_redis_service=redis_service.service_name,
                shared_allocation=matrix_allocation,
                secret_refs=secret_refs,
                health_check=MatrixHealthCheck(url=health_url, passed=None),
                notes=(
                    f"Matrix service '{service_name}' will be exposed at '{hostname}'.",
                    "Matrix will reuse shared Postgres "
                    f"'{postgres_service.service_name}' and shared Redis "
                    f"'{redis_service.service_name}'.",
                    "Matrix success in non-dry-run mode is gated on a backend health check.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = MatrixResourceRecord(
        resource_id=service_id, resource_name=service.resource_name
    )
    health_passed = backend.check_health(service=service_record, url=health_url)
    if not health_passed:
        raise MatrixError(f"Matrix health check failed for '{health_url}'.")

    return MatrixPhase(
        result=MatrixResult(
            outcome="applied"
            if "create" in {service.action, persistent_data.action}
            else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            shared_postgres_service=postgres_service.service_name,
            shared_redis_service=redis_service.service_name,
            shared_allocation=matrix_allocation,
            secret_refs=secret_refs,
            health_check=MatrixHealthCheck(url=health_url, passed=True),
            notes=(
                f"Matrix runtime '{service_name}' is reconciled and healthy.",
                "Matrix secret refs are deterministic names only; secret values are not persisted.",
            ),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_matrix_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    service_resource_id: str | None,
    data_resource_id: str | None,
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if not (
            (
                resource.resource_type == MATRIX_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == MATRIX_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=MATRIX_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=MATRIX_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _resolve_matrix_service(
    *,
    dry_run: bool,
    service_name: str,
    hostname: str,
    secret_refs: tuple[str, ...],
    shared_allocation: PackSharedAllocation,
    postgres_service_name: str,
    redis_service_name: str,
    data_resource_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: MatrixBackend,
) -> tuple[MatrixManagedResource, str]:
    return _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=service_name,
        resource_type=MATRIX_SERVICE_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=MATRIX_SERVICE_RESOURCE_TYPE,
            scope=_service_scope(stack_name),
        ),
        get_resource=backend.get_service,
        find_by_name=backend.find_service_by_name,
        create_resource=lambda resource_name: backend.create_service(
            resource_name=resource_name,
            hostname=hostname,
            secret_refs=secret_refs,
            shared_allocation=shared_allocation,
            postgres_service_name=postgres_service_name,
            redis_service_name=redis_service_name,
            data_resource_name=data_resource_name,
        ),
        collision_message=(
            "Existing Matrix service matched the desired name but is not wizard-owned."
        ),
    )


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], MatrixResourceRecord | None],
    find_by_name: Callable[[str], MatrixResourceRecord | None],
    create_resource: Callable[[str], MatrixResourceRecord],
    collision_message: str,
) -> tuple[MatrixManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise MatrixError(
                "Ownership ledger says Matrix resource "
                f"'{resource_type}' exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise MatrixError(
                "Ownership ledger Matrix resource "
                f"'{resource_type}' no longer matches the desired naming convention."
            )
        return (
            MatrixManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    existing = find_by_name(resource_name)
    if existing is not None:
        existing = create_resource(resource_name)
        return (
            MatrixManagedResource(
                action="reuse_existing",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    if dry_run:
        planned_id = f"planned:{resource_name}"
        return (
            MatrixManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return (
        MatrixManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _find_owned_resource(
    ownership_ledger: OwnershipLedger,
    resource_type: str,
    scope: str,
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == resource_type and resource.scope == scope
    ]
    if len(matches) > 1:
        raise MatrixError(
            "Ownership ledger contains multiple Matrix resources "
            f"'{resource_type}' for scope '{scope}'."
        )
    return matches[0] if matches else None


def _get_matrix_allocation(desired_state: DesiredState) -> PackSharedAllocation | None:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "matrix"
    ]
    if len(matches) > 1:
        raise MatrixError(
            "Desired state contains multiple shared-core allocations for the Matrix pack."
        )
    return matches[0] if matches else None


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-matrix"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-matrix-data"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:matrix-service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:matrix-data"


def _secret_refs(stack_name: str) -> tuple[str, ...]:
    return (
        f"{stack_name}-matrix-registration-shared-secret",
        f"{stack_name}-matrix-macaroon-secret-key",
    )


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MatrixError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _http_health_check(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host, _, path = url.removeprefix("https://").partition("/")
    request_path = "/" + path if path else "/"
    connection: http.client.HTTPSConnection | None = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=2.0)
        connection.request("GET", request_path)
        response = connection.getresponse()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()
