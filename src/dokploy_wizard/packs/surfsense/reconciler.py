# ruff: noqa: E501
"""SurfSense runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.packs.surfsense.models import (
    SurfSenseBootstrapState,
    SurfSenseHealthCheck,
    SurfSenseManagedResource,
    SurfSensePhase,
    SurfSensePostgresBinding,
    SurfSenseRedisBinding,
    SurfSenseResourceRecord,
    SurfSenseResult,
    SurfSenseServiceConfig,
    SurfSenseServiceEndpoints,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

SURFSENSE_SERVICE_RESOURCE_TYPE = "surfsense_service"
SURFSENSE_DATA_RESOURCE_TYPE = "surfsense_data"


class SurfSenseError(RuntimeError):
    """Raised when SurfSense reconciliation fails or detects drift."""


class SurfSenseBackend(Protocol):
    def get_service(self, resource_id: str) -> SurfSenseResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        frontend_hostname: str,
        api_hostname: str,
        zero_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        data_resource_name: str,
    ) -> SurfSenseResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        frontend_hostname: str,
        api_hostname: str,
        zero_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        data_resource_name: str,
    ) -> SurfSenseResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> SurfSenseResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> SurfSenseResourceRecord: ...

    def check_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool: ...

    def check_internal_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(self) -> tuple[SurfSenseBootstrapState, tuple[str, ...]]: ...


class ShellSurfSenseBackend:
    def __init__(self) -> None:
        self._service: SurfSenseResourceRecord | None = None
        self._data: SurfSenseResourceRecord | None = None

    def get_service(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return SurfSenseResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        frontend_hostname: str,
        api_hostname: str,
        zero_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        data_resource_name: str,
    ) -> SurfSenseResourceRecord:
        del (
            frontend_hostname,
            api_hostname,
            zero_hostname,
            postgres_service_name,
            redis_service_name,
            postgres,
            redis,
            data_resource_name,
        )
        self._service = SurfSenseResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        frontend_hostname: str,
        api_hostname: str,
        zero_hostname: str,
        postgres_service_name: str,
        redis_service_name: str,
        postgres: SharedPostgresAllocation,
        redis: SharedRedisAllocation,
        data_resource_name: str,
    ) -> SurfSenseResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            frontend_hostname=frontend_hostname,
            api_hostname=api_hostname,
            zero_hostname=zero_hostname,
            postgres_service_name=postgres_service_name,
            redis_service_name=redis_service_name,
            postgres=postgres,
            redis=redis,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return SurfSenseResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> SurfSenseResourceRecord:
        self._data = SurfSenseResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def check_internal_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(self) -> tuple[SurfSenseBootstrapState, tuple[str, ...]]:
        return (SurfSenseBootstrapState(created=None, verified_existing=None), ())


def reconcile_surfsense(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: SurfSenseBackend,
) -> SurfSensePhase:
    if "surfsense" not in desired_state.enabled_packs:
        return SurfSensePhase(
            result=SurfSenseResult(
                outcome="skipped",
                enabled=False,
                frontend_hostname=None,
                api_hostname=None,
                zero_hostname=None,
                service=None,
                persistent_data=None,
                bootstrap_state=None,
                health_check=None,
                config=None,
                notes=("SurfSense pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    frontend_hostname = desired_state.hostnames.get("surfsense")
    api_hostname = desired_state.hostnames.get("surfsense-api")
    zero_hostname = desired_state.hostnames.get("surfsense-zero")
    if frontend_hostname is None or api_hostname is None or zero_hostname is None:
        raise SurfSenseError("Desired state is missing one or more canonical SurfSense hostnames.")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "surfsense"),
        None,
    )
    if (
        allocation is None
        or allocation.postgres is None
        or allocation.redis is None
        or desired_state.shared_core.postgres is None
        or desired_state.shared_core.redis is None
    ):
        raise SurfSenseError("SurfSense shared-core postgres/redis allocation is missing from desired state.")

    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    health_url = f"https://{api_hostname}/ready"

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=SURFSENSE_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=SURFSENSE_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
    )
    service, service_id = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        frontend_hostname=frontend_hostname,
        api_hostname=api_hostname,
        zero_hostname=zero_hostname,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        data_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    config = SurfSenseServiceConfig(
        endpoints=SurfSenseServiceEndpoints(
            frontend_url=f"https://{frontend_hostname}",
            api_url=f"https://{api_hostname}",
            zero_url=f"https://{zero_hostname}",
        ),
        postgres=SurfSensePostgresBinding(
            database_name=allocation.postgres.database_name,
            user_name=allocation.postgres.user_name,
            password_secret_ref=allocation.postgres.password_secret_ref,
        ),
        redis=SurfSenseRedisBinding(url_secret_ref=allocation.redis.password_secret_ref),
    )

    if dry_run:
        return SurfSensePhase(
            result=SurfSenseResult(
                outcome="plan_only",
                enabled=True,
                frontend_hostname=frontend_hostname,
                api_hostname=api_hostname,
                zero_hostname=zero_hostname,
                service=service,
                persistent_data=persistent_data,
                bootstrap_state=SurfSenseBootstrapState(created=None, verified_existing=None),
                health_check=SurfSenseHealthCheck(url=health_url, path="/ready", passed=None),
                config=config,
                notes=(
                    f"SurfSense service '{service_name}' will expose frontend '{frontend_hostname}', API '{api_hostname}', and Zero '{zero_hostname}'.",
                    f"SurfSense will reuse shared-core postgres database '{allocation.postgres.database_name}' and Redis identity '{allocation.redis.identity_name}'.",
                    "SurfSense success in non-dry-run mode is gated on API readiness and first-user bootstrap.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = SurfSenseResourceRecord(resource_id=service_id, resource_name=service.resource_name)
    initial_health = backend.check_health(service=service_record, url=health_url)
    bootstrap_state, bootstrap_notes = backend.ensure_application_ready()
    final_health = initial_health or backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise SurfSenseError(f"SurfSense health check failed for '{health_url}'.")
    notes = list(bootstrap_notes)
    notes.extend(
        (
            f"SurfSense service '{service_name}' is reconciled and healthy.",
            f"SurfSense data persists in '{data_name}'.",
        )
    )
    return SurfSensePhase(
        result=SurfSenseResult(
            outcome="applied" if "create" in {service.action, persistent_data.action} else "already_present",
            enabled=True,
            frontend_hostname=frontend_hostname,
            api_hostname=api_hostname,
            zero_hostname=zero_hostname,
            service=service,
            persistent_data=persistent_data,
            bootstrap_state=bootstrap_state,
            health_check=SurfSenseHealthCheck(url=health_url, path="/ready", passed=True),
            config=config,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_surfsense_ledger(
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
            (resource.resource_type == SURFSENSE_SERVICE_RESOURCE_TYPE and resource.scope == _service_scope(stack_name))
            or (resource.resource_type == SURFSENSE_DATA_RESOURCE_TYPE and resource.scope == _data_scope(stack_name))
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SURFSENSE_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SURFSENSE_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(format_version=existing_ledger.format_version, resources=tuple(resources))


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], SurfSenseResourceRecord | None],
    find_by_name: Callable[[str], SurfSenseResourceRecord | None],
    create_resource: Callable[[str], SurfSenseResourceRecord],
) -> tuple[SurfSenseManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise SurfSenseError(
                f"Ownership ledger says the SurfSense {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise SurfSenseError(
                f"Ownership ledger SurfSense {resource_type} no longer matches the desired naming convention."
            )
        return SurfSenseManagedResource("reuse_owned", existing.resource_id, existing.resource_name), existing.resource_id
    existing = find_by_name(resource_name)
    if existing is not None:
        if dry_run:
            return SurfSenseManagedResource("reuse_existing", existing.resource_id, existing.resource_name), existing.resource_id
        created = create_resource(resource_name)
        return SurfSenseManagedResource("reuse_existing", created.resource_id, created.resource_name), created.resource_id
    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return SurfSenseManagedResource("create", planned_id, resource_name), planned_id
    created = create_resource(resource_name)
    return SurfSenseManagedResource("create", created.resource_id, created.resource_name), created.resource_id


def _resolve_service(
    *,
    dry_run: bool,
    service_name: str,
    frontend_hostname: str,
    api_hostname: str,
    zero_hostname: str,
    postgres_service_name: str,
    redis_service_name: str,
    postgres: SharedPostgresAllocation,
    redis: SharedRedisAllocation,
    data_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: SurfSenseBackend,
) -> tuple[SurfSenseManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=SURFSENSE_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise SurfSenseError(
                "Ownership ledger says the SurfSense service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise SurfSenseError("Ownership ledger SurfSense service no longer matches the desired naming convention.")
        if dry_run:
            return SurfSenseManagedResource("reuse_owned", existing.resource_id, existing.resource_name), existing.resource_id
        updated = backend.update_service(
            resource_id=existing.resource_id,
            resource_name=service_name,
            frontend_hostname=frontend_hostname,
            api_hostname=api_hostname,
            zero_hostname=zero_hostname,
            postgres_service_name=postgres_service_name,
            redis_service_name=redis_service_name,
            postgres=postgres,
            redis=redis,
            data_resource_name=data_name,
        )
        return SurfSenseManagedResource("update_owned", updated.resource_id, updated.resource_name), updated.resource_id
    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if dry_run:
            return SurfSenseManagedResource("reuse_existing", existing.resource_id, existing.resource_name), existing.resource_id
        created = backend.create_service(
            resource_name=service_name,
            frontend_hostname=frontend_hostname,
            api_hostname=api_hostname,
            zero_hostname=zero_hostname,
            postgres_service_name=postgres_service_name,
            redis_service_name=redis_service_name,
            postgres=postgres,
            redis=redis,
            data_resource_name=data_name,
        )
        return SurfSenseManagedResource("reuse_existing", created.resource_id, created.resource_name), created.resource_id
    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return SurfSenseManagedResource("create", planned_id, service_name), planned_id
    created = backend.create_service(
        resource_name=service_name,
        frontend_hostname=frontend_hostname,
        api_hostname=api_hostname,
        zero_hostname=zero_hostname,
        postgres_service_name=postgres_service_name,
        redis_service_name=redis_service_name,
        postgres=postgres,
        redis=redis,
        data_resource_name=data_name,
    )
    return SurfSenseManagedResource("create", created.resource_id, created.resource_name), created.resource_id


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
    return f"{stack_name}-surfsense"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-surfsense-data"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:surfsense:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:surfsense:data"


def _http_health_check(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection("127.0.0.1", 443, timeout=10)
    try:
        connection.request("GET", parsed.path or "/", headers={"Host": parsed.netloc})
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()
