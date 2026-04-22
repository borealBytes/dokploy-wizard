"""Moodle runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import PackSharedAllocation, SharedPostgresAllocation
from dokploy_wizard.packs.moodle.models import (
    MoodleHealthCheck,
    MoodleManagedResource,
    MoodlePhase,
    MoodlePostgresBinding,
    MoodleResourceRecord,
    MoodleResult,
    MoodleServiceConfig,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

MOODLE_SERVICE_RESOURCE_TYPE = "moodle_service"
MOODLE_DATA_RESOURCE_TYPE = "moodle_data"


class MoodleError(RuntimeError):
    """Raised when Moodle reconciliation fails or detects drift."""


class MoodleBackend(Protocol):
    def get_service(self, resource_id: str) -> MoodleResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> MoodleResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> MoodleResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> MoodleResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> MoodleResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> MoodleResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> MoodleResourceRecord: ...

    def check_health(self, *, service: MoodleResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(self) -> tuple[str, ...]: ...


class ShellMoodleBackend:
    def __init__(self) -> None:
        self._service: MoodleResourceRecord | None = None
        self._data: MoodleResourceRecord | None = None

    def get_service(self, resource_id: str) -> MoodleResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return MoodleResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> MoodleResourceRecord:
        del hostname, postgres_service_name, postgres, data_resource_name
        self._service = MoodleResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> MoodleResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> MoodleResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return MoodleResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> MoodleResourceRecord:
        self._data = MoodleResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: MoodleResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(self) -> tuple[str, ...]:
        return ()


def reconcile_moodle(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: MoodleBackend,
) -> MoodlePhase:
    if "moodle" not in desired_state.enabled_packs:
        return MoodlePhase(
            result=MoodleResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                health_check=None,
                config=None,
                notes=("Moodle pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("moodle")
    if hostname is None:
        raise MoodleError(
            "Desired state is missing the canonical Moodle hostname at hostnames['moodle']."
        )

    allocation, postgres_service_name, postgres = _get_moodle_allocation(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    access_url = f"https://{hostname}"
    health_url = f"{access_url}/login/index.php"

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=MOODLE_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=MOODLE_DATA_RESOURCE_TYPE,
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
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    config = MoodleServiceConfig(
        access_url=access_url,
        postgres=MoodlePostgresBinding(
            database_name=postgres.database_name,
            user_name=postgres.user_name,
            password_secret_ref=postgres.password_secret_ref,
        ),
    )

    if dry_run:
        return MoodlePhase(
            result=MoodleResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                health_check=MoodleHealthCheck(url=health_url, passed=None),
                config=config,
                notes=(
                    f"Moodle service '{service_name}' will be exposed at '{hostname}'.",
                    f"Moodle will reuse shared-core postgres database '{postgres.database_name}'.",
                    "Moodle success in non-dry-run mode is gated on backend readiness hooks and the login endpoint health check.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = MoodleResourceRecord(resource_id=service_id, resource_name=service.resource_name)
    initial_health = backend.check_health(service=service_record, url=health_url)
    notes = list(backend.ensure_application_ready())
    if initial_health:
        final_health = True
    else:
        final_health = backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise MoodleError(f"Moodle health check failed for '{health_url}'.")

    notes.extend(
        (
            f"Moodle service '{service_name}' is reconciled and healthy.",
            f"Moodle data persists in '{data_name}'.",
        )
    )
    return MoodlePhase(
        result=MoodleResult(
            outcome="applied"
            if "create" in {service.action, persistent_data.action}
            else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            health_check=MoodleHealthCheck(url=health_url, passed=True),
            config=config,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_moodle_ledger(
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
                resource.resource_type == MOODLE_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == MOODLE_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=MOODLE_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=MOODLE_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _get_moodle_allocation(
    desired_state: DesiredState,
) -> tuple[PackSharedAllocation, str, SharedPostgresAllocation]:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "moodle"
    ]
    if len(matches) != 1 or matches[0].postgres is None or desired_state.shared_core.postgres is None:
        raise MoodleError("Moodle shared-core postgres allocation is missing from desired state.")
    return matches[0], desired_state.shared_core.postgres.service_name, matches[0].postgres


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], MoodleResourceRecord | None],
    find_by_name: Callable[[str], MoodleResourceRecord | None],
    create_resource: Callable[[str], MoodleResourceRecord],
) -> tuple[MoodleManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise MoodleError(
                f"Ownership ledger says the Moodle {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise MoodleError(
                f"Ownership ledger Moodle {resource_type} no longer matches the desired naming convention."
            )
        return (
            MoodleManagedResource(
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
                MoodleManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = create_resource(resource_name)
        return (
            MoodleManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            MoodleManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return (
        MoodleManagedResource(
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
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    data_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: MoodleBackend,
) -> tuple[MoodleManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=MOODLE_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise MoodleError(
                "Ownership ledger says the Moodle service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise MoodleError(
                "Ownership ledger Moodle service no longer matches the desired naming convention."
            )
        if dry_run:
            return (
                MoodleManagedResource(
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
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
        )
        return (
            MoodleManagedResource(
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
                MoodleManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
        )
        return (
            MoodleManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return (
            MoodleManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=service_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_resource_name=data_name,
    )
    return (
        MoodleManagedResource(
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
    return f"{stack_name}-moodle"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-moodle-data"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:moodle:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:moodle:data"


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
