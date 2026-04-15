"""SeaweedFS runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Protocol

from dokploy_wizard.packs.seaweedfs.models import (
    SeaweedFsHealthCheck,
    SeaweedFsManagedResource,
    SeaweedFsPhase,
    SeaweedFsResult,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput

SEAWEEDFS_SERVICE_RESOURCE_TYPE = "seaweedfs_service"
SEAWEEDFS_DATA_RESOURCE_TYPE = "seaweedfs_data"


class SeaweedFsError(RuntimeError):
    """Raised when SeaweedFS reconciliation fails or detects drift."""


class SeaweedFsBackend(Protocol):
    def get_service(self, resource_id: str) -> SeaweedFsResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> SeaweedFsResourceRecord | None: ...

    def find_persistent_data_by_name(
        self, resource_name: str
    ) -> SeaweedFsResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> SeaweedFsResourceRecord: ...

    def check_health(self, *, service: SeaweedFsResourceRecord, url: str) -> bool: ...


class SeaweedFsResourceRecord:
    def __init__(self, *, resource_id: str, resource_name: str) -> None:
        self.resource_id = resource_id
        self.resource_name = resource_name


class ShellSeaweedFsBackend:
    """Deterministic default backend for SeaweedFS runtime planning and health checks."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_existing_service_id = values.get("SEAWEEDFS_MOCK_EXISTING_SERVICE_ID")
        self._forced_existing_data_id = values.get("SEAWEEDFS_MOCK_EXISTING_DATA_ID")
        self._forced_health = _optional_bool(values, "SEAWEEDFS_MOCK_HEALTHY")
        self._service: SeaweedFsResourceRecord | None = None
        self._data: SeaweedFsResourceRecord | None = None

    def get_service(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        if self._forced_existing_service_id == resource_id:
            return SeaweedFsResourceRecord(resource_id=resource_id, resource_name=resource_id)
        return SeaweedFsResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        if self._forced_existing_service_id is None:
            return None
        return SeaweedFsResourceRecord(
            resource_id=self._forced_existing_service_id,
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        del hostname, access_key, secret_key, data_resource_name
        self._service = SeaweedFsResourceRecord(
            resource_id=resource_name, resource_name=resource_name
        )
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        access_key: str,
        secret_key: str,
        data_resource_name: str,
    ) -> SeaweedFsResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            access_key=access_key,
            secret_key=secret_key,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> SeaweedFsResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        if self._forced_existing_data_id == resource_id:
            return SeaweedFsResourceRecord(resource_id=resource_id, resource_name=resource_id)
        return SeaweedFsResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> SeaweedFsResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        if self._forced_existing_data_id is None:
            return None
        return SeaweedFsResourceRecord(
            resource_id=self._forced_existing_data_id,
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> SeaweedFsResourceRecord:
        self._data = SeaweedFsResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: SeaweedFsResourceRecord, url: str) -> bool:
        del service
        if self._forced_health is not None:
            return self._forced_health
        return _http_health_check(url)


def reconcile_seaweedfs(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: SeaweedFsBackend,
) -> SeaweedFsPhase:
    if "seaweedfs" not in desired_state.enabled_packs:
        return SeaweedFsPhase(
            result=SeaweedFsResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                access_key=None,
                health_check=None,
                notes=("SeaweedFS pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("s3")
    if hostname is None:
        raise SeaweedFsError(
            "Desired state is missing the canonical SeaweedFS hostname at hostnames['s3']."
        )
    access_key = desired_state.seaweedfs_access_key
    secret_key = desired_state.seaweedfs_secret_key
    if access_key is None or secret_key is None:
        raise SeaweedFsError(
            "SeaweedFS access/secret key configuration is missing from desired state."
        )

    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    health_url = f"https://{hostname}/status"

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=SEAWEEDFS_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=SEAWEEDFS_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
        collision_message=(
            "Existing SeaweedFS persistent data matched the desired name but is not wizard-owned."
        ),
    )
    service, service_id = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        access_key=access_key,
        secret_key=secret_key,
        data_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    if dry_run:
        return SeaweedFsPhase(
            result=SeaweedFsResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                access_key=access_key,
                health_check=SeaweedFsHealthCheck(url=health_url, passed=None),
                notes=(
                    f"SeaweedFS service '{service_name}' will expose S3 at '{hostname}'.",
                    "SeaweedFS success in non-dry-run mode is gated on an S3 "
                    "endpoint health check.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = SeaweedFsResourceRecord(
        resource_id=service_id, resource_name=service.resource_name
    )
    if not backend.check_health(service=service_record, url=health_url):
        raise SeaweedFsError(f"SeaweedFS health check failed for '{health_url}'.")

    return SeaweedFsPhase(
        result=SeaweedFsResult(
            outcome="applied"
            if "create" in {service.action, persistent_data.action}
            else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            access_key=access_key,
            health_check=SeaweedFsHealthCheck(url=health_url, passed=True),
            notes=(
                f"SeaweedFS runtime '{service_name}' is reconciled and healthy.",
                "SeaweedFS credentials are provided from desired state only; "
                "no integrations are wired yet.",
            ),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_seaweedfs_ledger(
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
                resource.resource_type == SEAWEEDFS_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == SEAWEEDFS_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SEAWEEDFS_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SEAWEEDFS_DATA_RESOURCE_TYPE,
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
    get_resource: Callable[[str], SeaweedFsResourceRecord | None],
    find_by_name: Callable[[str], SeaweedFsResourceRecord | None],
    create_resource: Callable[[str], SeaweedFsResourceRecord],
    collision_message: str,
) -> tuple[SeaweedFsManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise SeaweedFsError(
                f"Ownership ledger says the {resource_type} resource exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise SeaweedFsError(
                f"Ownership ledger {resource_type} no longer matches the desired naming convention."
            )
        return (
            SeaweedFsManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )
    existing = find_by_name(resource_name)
    if existing is not None:
        if existing.resource_id.startswith("dokploy-compose:"):
            return (
                SeaweedFsManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        raise SeaweedFsError(collision_message)
    if dry_run:
        planned_id = f"planned:{resource_name}"
        return (
            SeaweedFsManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )
    created = create_resource(resource_name)
    return (
        SeaweedFsManagedResource(
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
    access_key: str,
    secret_key: str,
    data_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: SeaweedFsBackend,
) -> tuple[SeaweedFsManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=SEAWEEDFS_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise SeaweedFsError(
                "Ownership ledger says the seaweedfs_service resource exists, "
                "but the backend could not find it."
            )
        if not dry_run:
            updated = backend.update_service(
                resource_id=existing.resource_id,
                resource_name=service_name,
                hostname=hostname,
                access_key=access_key,
                secret_key=secret_key,
                data_resource_name=data_name,
            )
            return (
                SeaweedFsManagedResource(
                    action="update_owned",
                    resource_id=updated.resource_id,
                    resource_name=updated.resource_name,
                ),
                updated.resource_id,
            )
        return (
            SeaweedFsManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if existing.resource_id.startswith("dokploy-compose:"):
            if not dry_run:
                updated = backend.update_service(
                    resource_id=existing.resource_id,
                    resource_name=service_name,
                    hostname=hostname,
                    access_key=access_key,
                    secret_key=secret_key,
                    data_resource_name=data_name,
                )
                return (
                    SeaweedFsManagedResource(
                        action="reuse_existing",
                        resource_id=updated.resource_id,
                        resource_name=updated.resource_name,
                    ),
                    updated.resource_id,
                )
            return (
                SeaweedFsManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        raise SeaweedFsError(
            "Existing SeaweedFS service matched the desired name but is not wizard-owned."
        )

    if dry_run:
        planned_id = f"planned:{service_name}"
        return (
            SeaweedFsManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=service_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        access_key=access_key,
        secret_key=secret_key,
        data_resource_name=data_name,
    )
    return (
        SeaweedFsManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _find_owned_resource(
    *, ownership_ledger: OwnershipLedger, resource_type: str, scope: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == resource_type and resource.scope == scope
    ]
    if len(matches) > 1:
        raise SeaweedFsError(
            f"Ownership ledger contains multiple SeaweedFS resources for scope '{scope}'."
        )
    return matches[0] if matches else None


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs-data"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:seaweedfs-service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:seaweedfs-data"


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SeaweedFsError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _http_health_check(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host = url.removeprefix("https://").split("/", 1)[0]
    connection: http.client.HTTPSConnection | None = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=2.0)
        connection.request("GET", "/status")
        response = connection.getresponse()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()
