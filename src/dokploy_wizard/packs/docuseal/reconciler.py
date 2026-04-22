"""DocuSeal runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import PackSharedAllocation, SharedPostgresAllocation
from dokploy_wizard.packs.docuseal.models import (
    DocuSealBootstrapState,
    DocuSealHealthState,
    DocuSealManagedResource,
    DocuSealPhase,
    DocuSealPostgresBinding,
    DocuSealResourceRecord,
    DocuSealResult,
    DocuSealServiceConfig,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

DOCUSEAL_SERVICE_RESOURCE_TYPE = "docuseal_service"
DOCUSEAL_DATA_RESOURCE_TYPE = "docuseal_data"


class DocuSealError(RuntimeError):
    """Raised when DocuSeal reconciliation fails or detects drift."""


class DocuSealBackend(Protocol):
    def get_service(self, resource_id: str) -> DocuSealResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> DocuSealResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> DocuSealResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> DocuSealResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> DocuSealResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> DocuSealResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> DocuSealResourceRecord: ...

    def check_health(self, *, service: DocuSealResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(
        self, *, secret_key_base_secret_ref: str
    ) -> tuple[DocuSealBootstrapState, tuple[str, ...]]: ...


class ShellDocuSealBackend:
    def __init__(self) -> None:
        self._service: DocuSealResourceRecord | None = None
        self._data: DocuSealResourceRecord | None = None

    def get_service(self, resource_id: str) -> DocuSealResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return DocuSealResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
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
    ) -> DocuSealResourceRecord:
        del hostname, postgres_service_name, postgres, data_resource_name
        self._service = DocuSealResourceRecord(
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
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> DocuSealResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> DocuSealResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return DocuSealResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> DocuSealResourceRecord:
        self._data = DocuSealResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: DocuSealResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(
        self, *, secret_key_base_secret_ref: str
    ) -> tuple[DocuSealBootstrapState, tuple[str, ...]]:
        return (
            DocuSealBootstrapState(
                initialized=True,
                secret_key_base_secret_ref=secret_key_base_secret_ref,
            ),
            (),
        )


def reconcile_docuseal(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: DocuSealBackend,
) -> DocuSealPhase:
    if "docuseal" not in desired_state.enabled_packs:
        return DocuSealPhase(
            result=DocuSealResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                bootstrap_state=None,
                health_state=None,
                config=None,
                notes=("DocuSeal pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("docuseal")
    if hostname is None:
        raise DocuSealError(
            "Desired state is missing the canonical DocuSeal hostname at hostnames['docuseal']."
        )

    _, postgres_service_name, postgres = _get_docuseal_allocation(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    access_url = f"https://{hostname}"
    health_url = f"{access_url}/up"
    secret_key_base_secret_ref = _secret_key_base_secret_ref(desired_state.stack_name)

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=DOCUSEAL_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=DOCUSEAL_DATA_RESOURCE_TYPE,
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

    config = DocuSealServiceConfig(
        access_url=access_url,
        postgres=DocuSealPostgresBinding(
            database_name=postgres.database_name,
            user_name=postgres.user_name,
            password_secret_ref=postgres.password_secret_ref,
        ),
    )

    if dry_run:
        return DocuSealPhase(
            result=DocuSealResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                bootstrap_state=DocuSealBootstrapState(
                    initialized=None,
                    secret_key_base_secret_ref=secret_key_base_secret_ref,
                ),
                health_state=DocuSealHealthState(url=health_url, path="/up", passed=None),
                config=config,
                notes=(
                    f"DocuSeal service '{service_name}' will be exposed at '{hostname}'.",
                    f"DocuSeal will reuse shared-core postgres database '{postgres.database_name}'.",
                    "DocuSeal success in non-dry-run mode is gated on bootstrap readiness metadata and the /up health endpoint.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = DocuSealResourceRecord(resource_id=service_id, resource_name=service.resource_name)
    initial_health = backend.check_health(service=service_record, url=health_url)
    bootstrap_state, bootstrap_notes = backend.ensure_application_ready(
        secret_key_base_secret_ref=secret_key_base_secret_ref
    )
    if initial_health:
        final_health = True
    else:
        final_health = backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise DocuSealError(f"DocuSeal health check failed for '{health_url}'.")

    notes = list(bootstrap_notes)
    notes.extend(
        (
            f"DocuSeal service '{service_name}' is reconciled and healthy.",
            f"DocuSeal data persists in '{data_name}'.",
        )
    )
    return DocuSealPhase(
        result=DocuSealResult(
            outcome="applied" if "create" in {service.action, persistent_data.action} else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            bootstrap_state=bootstrap_state,
            health_state=DocuSealHealthState(url=health_url, path="/up", passed=True),
            config=config,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_docuseal_ledger(
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
                resource.resource_type == DOCUSEAL_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == DOCUSEAL_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=DOCUSEAL_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=DOCUSEAL_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _get_docuseal_allocation(
    desired_state: DesiredState,
) -> tuple[PackSharedAllocation, str, SharedPostgresAllocation]:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "docuseal"
    ]
    if (
        len(matches) != 1
        or matches[0].postgres is None
        or desired_state.shared_core.postgres is None
    ):
        raise DocuSealError("DocuSeal shared-core postgres allocation is missing from desired state.")
    return matches[0], desired_state.shared_core.postgres.service_name, matches[0].postgres


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], DocuSealResourceRecord | None],
    find_by_name: Callable[[str], DocuSealResourceRecord | None],
    create_resource: Callable[[str], DocuSealResourceRecord],
) -> tuple[DocuSealManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise DocuSealError(
                f"Ownership ledger says the DocuSeal {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise DocuSealError(
                f"Ownership ledger DocuSeal {resource_type} no longer matches the desired naming convention."
            )
        return (
            DocuSealManagedResource(
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
                DocuSealManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = create_resource(resource_name)
        return (
            DocuSealManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            DocuSealManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return (
        DocuSealManagedResource(
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
    backend: DocuSealBackend,
) -> tuple[DocuSealManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=DOCUSEAL_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise DocuSealError(
                "Ownership ledger says the DocuSeal service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise DocuSealError(
                "Ownership ledger DocuSeal service no longer matches the desired naming convention."
            )
        if dry_run:
            return (
                DocuSealManagedResource(
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
            DocuSealManagedResource(
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
                DocuSealManagedResource(
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
            DocuSealManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return (
            DocuSealManagedResource(
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
        DocuSealManagedResource(
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
    return f"{stack_name}-docuseal"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-docuseal-data"


def _secret_key_base_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-docuseal-secret-key-base"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:docuseal:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:docuseal:data"


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
