"""Coder runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.packs.coder.models import (
    CoderHealthCheck,
    CoderManagedResource,
    CoderPhase,
    CoderPostgresBinding,
    CoderResourceRecord,
    CoderResult,
    CoderServiceConfig,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

CODER_SERVICE_RESOURCE_TYPE = "coder_service"
CODER_DATA_RESOURCE_TYPE = "coder_data"


class CoderError(RuntimeError):
    """Raised when Coder reconciliation fails or detects drift."""


class CoderBackend(Protocol):
    def get_service(self, resource_id: str) -> CoderResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        wildcard_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> CoderResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        wildcard_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> CoderResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord: ...

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(self) -> tuple[str, ...]: ...


class ShellCoderBackend:
    def __init__(self) -> None:
        self._service: CoderResourceRecord | None = None
        self._data: CoderResourceRecord | None = None

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return CoderResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        wildcard_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> CoderResourceRecord:
        del hostname, wildcard_hostname, postgres_service_name, postgres, data_resource_name
        self._service = CoderResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        wildcard_hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> CoderResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            wildcard_hostname=wildcard_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return CoderResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        self._data = CoderResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(self) -> tuple[str, ...]:
        return ()


def reconcile_coder(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: CoderBackend,
) -> CoderPhase:
    if "coder" not in desired_state.enabled_packs:
        return CoderPhase(
            result=CoderResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                wildcard_hostname=None,
                service=None,
                persistent_data=None,
                health_check=None,
                config=None,
                notes=("Coder pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("coder")
    wildcard_hostname = desired_state.hostnames.get("coder-wildcard")
    if hostname is None or wildcard_hostname is None:
        raise CoderError("Desired state is missing the canonical Coder hostnames.")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "coder"),
        None,
    )
    if (
        allocation is None
        or allocation.postgres is None
        or desired_state.shared_core.postgres is None
    ):
        raise CoderError("Coder shared-core postgres allocation is missing from desired state.")

    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    health_url = f"https://{hostname}/healthz"

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=CODER_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=CODER_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
        collision_message="Existing Coder persistent data matched the desired name but is not wizard-owned.",
    )
    service, service_id = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        wildcard_hostname=wildcard_hostname,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=allocation.postgres,
        data_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    config = CoderServiceConfig(
        access_url=f"https://{hostname}",
        wildcard_access_url=wildcard_hostname,
        postgres=CoderPostgresBinding(
            database_name=allocation.postgres.database_name,
            user_name=allocation.postgres.user_name,
            password_secret_ref=allocation.postgres.password_secret_ref,
        ),
    )

    if dry_run:
        return CoderPhase(
            result=CoderResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                wildcard_hostname=wildcard_hostname,
                service=service,
                persistent_data=persistent_data,
                health_check=CoderHealthCheck(url=health_url, passed=None),
                config=config,
                notes=(
                    f"Coder service '{service_name}' will be exposed at '{hostname}'.",
                    f"Wildcard workspace routing will use '{wildcard_hostname}'.",
                    "Coder success in non-dry-run mode is gated on the /healthz endpoint.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = CoderResourceRecord(
        resource_id=service_id, resource_name=service.resource_name
    )
    initial_health = backend.check_health(service=service_record, url=health_url)
    notes = list(backend.ensure_application_ready())
    if initial_health:
        final_health = True
    else:
        final_health = backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise CoderError(f"Coder health check failed for '{health_url}'.")
    notes.extend(
        (
            f"Coder service '{service_name}' is reconciled and healthy.",
            f"Wildcard workspace routing uses '{wildcard_hostname}'.",
        )
    )

    return CoderPhase(
        result=CoderResult(
            outcome="applied"
            if "create" in {service.action, persistent_data.action}
            else "already_present",
            enabled=True,
            hostname=hostname,
            wildcard_hostname=wildcard_hostname,
            service=service,
            persistent_data=persistent_data,
            health_check=CoderHealthCheck(url=health_url, passed=True),
            config=config,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_coder_ledger(
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
                resource.resource_type == CODER_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == CODER_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=CODER_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=CODER_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version, resources=tuple(resources)
    )


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], CoderResourceRecord | None],
    find_by_name: Callable[[str], CoderResourceRecord | None],
    create_resource: Callable[[str], CoderResourceRecord],
    collision_message: str,
) -> tuple[CoderManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise CoderError(
                f"Ownership ledger says the Coder {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise CoderError(
                f"Ownership ledger Coder {resource_type} no longer matches the desired naming convention."
            )
        return (
            CoderManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )
    existing = find_by_name(resource_name)
    if existing is not None:
        if dry_run:
            return (
                CoderManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = create_resource(resource_name)
        return (
            CoderManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )
    if dry_run:
        return (
            CoderManagedResource(
                action="create",
                resource_id=f"planned-{resource_type}:{resource_name}",
                resource_name=resource_name,
            ),
            f"planned-{resource_type}:{resource_name}",
        )
    created = create_resource(resource_name)
    return (
        CoderManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _resolve_service(
    *,
    dry_run: bool,
    service_name: str,
    hostname: str,
    wildcard_hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    data_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: CoderBackend,
) -> tuple[CoderManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=CODER_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise CoderError(
                "Ownership ledger says the Coder service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise CoderError(
                "Ownership ledger Coder service no longer matches the desired naming convention."
            )
        if dry_run:
            return (
                CoderManagedResource(
                    action="reuse_owned",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        updated = backend.update_service(
            resource_id=existing.resource_id,
            resource_name=service_name,
            hostname=hostname,
            wildcard_hostname=wildcard_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
        )
        return (
            CoderManagedResource(
                action="update_owned",
                resource_id=updated.resource_id,
                resource_name=updated.resource_name,
            ),
            updated.resource_id,
        )
    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if dry_run:
            return (
                CoderManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            wildcard_hostname=wildcard_hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
        )
        return (
            CoderManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )
    if dry_run:
        return (
            CoderManagedResource(
                action="create",
                resource_id=f"planned-service:{service_name}",
                resource_name=service_name,
            ),
            f"planned-service:{service_name}",
        )
    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        wildcard_hostname=wildcard_hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_resource_name=data_name,
    )
    return (
        CoderManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


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
    return f"{stack_name}-coder"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-coder-data"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:coder:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:coder:data"


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
