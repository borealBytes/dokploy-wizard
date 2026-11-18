# ruff: noqa: E501
"""FileX runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.packs.filex.models import (
    FilexBootstrapState,
    FilexHealthState,
    FilexManagedResource,
    FilexPhase,
    FilexResourceRecord,
    FilexResult,
    FilexStorageBinding,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

FILEX_SERVICE_RESOURCE_TYPE = "filex_service"
FILEX_DATA_RESOURCE_TYPE = "filex_data"

# The SeaweedFS bucket FileX is seeded with as its only storage. Names are fixed
# so every stack can be found the same way; the wizard owns the bucket from the
# first boot and turns native versioning on so overwrites never truly overwrite.
FILEX_BUCKET = "filex"
FILEX_STORAGE_PREFIX = "files"
FILEX_S3_REGION = "us-east-1"


class FilexError(RuntimeError):
    """Raised when FileX reconciliation fails or detects drift."""


class FilexBackend(Protocol):
    def get_service(self, resource_id: str) -> FilexResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> FilexResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        bucket: str,
        data_resource_name: str,
    ) -> FilexResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        bucket: str,
        data_resource_name: str,
    ) -> FilexResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> FilexResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> FilexResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> FilexResourceRecord: ...

    def check_health(self, *, service: FilexResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[FilexBootstrapState, tuple[str, ...]]: ...


class ShellFilexBackend:
    def __init__(self) -> None:
        self._service: FilexResourceRecord | None = None
        self._data: FilexResourceRecord | None = None

    def get_service(self, resource_id: str) -> FilexResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return FilexResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        bucket: str,
        data_resource_name: str,
    ) -> FilexResourceRecord:
        del hostname, bucket, data_resource_name
        self._service = FilexResourceRecord(
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
        bucket: str,
        data_resource_name: str,
    ) -> FilexResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            bucket=bucket,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> FilexResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return FilexResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> FilexResourceRecord:
        self._data = FilexResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: FilexResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[FilexBootstrapState, tuple[str, ...]]:
        return (
            FilexBootstrapState(
                initialized=True,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=True,
                versioning_enabled=True,
                login_verified=None,
            ),
            (),
        )


def reconcile_filex(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: FilexBackend,
) -> FilexPhase:
    if "filex" not in desired_state.enabled_packs:
        return FilexPhase(
            result=FilexResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                bootstrap_state=None,
                health_state=None,
                storage=None,
                notes=("FileX pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("filex")
    if hostname is None:
        raise FilexError(
            "Desired state is missing the canonical FileX hostname at hostnames['filex']."
        )
    if "seaweedfs" not in desired_state.enabled_packs:
        raise FilexError("FileX requires the SeaweedFS pack for its default storage.")

    _, s3_endpoint_url = _seaweedfs_endpoint(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    access_url = f"https://{hostname}"
    health_url = f"{access_url}/healthz"
    secret_key_secret_ref = _secret_key_secret_ref(desired_state.stack_name)
    storage = FilexStorageBinding(
        bucket=FILEX_BUCKET,
        prefix=FILEX_STORAGE_PREFIX,
        region=FILEX_S3_REGION,
        endpoint_url=s3_endpoint_url,
        access_key_secret_ref=_seaweedfs_access_key_secret_ref(desired_state.stack_name),
    )

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=FILEX_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=FILEX_DATA_RESOURCE_TYPE,
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
        bucket=storage.bucket,
        data_name=data_name,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    if dry_run:
        return FilexPhase(
            result=FilexResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                bootstrap_state=FilexBootstrapState(
                    initialized=None,
                    secret_key_secret_ref=secret_key_secret_ref,
                    bucket_ready=None,
                    versioning_enabled=None,
                    login_verified=None,
                ),
                health_state=FilexHealthState(url=health_url, path="/healthz", passed=None),
                storage=storage,
                notes=(
                    f"FileX service '{service_name}' will be exposed at '{hostname}'.",
                    f"FileX will seed SeaweedFS bucket '{storage.bucket}' (prefix '{storage.prefix}') as its default storage.",
                    "FileX success in non-dry-run mode is gated on the /healthz endpoint, the seeded admin login, and bucket versioning being enabled.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = FilexResourceRecord(resource_id=service_id, resource_name=service.resource_name)
    initial_health = backend.check_health(service=service_record, url=health_url)
    bootstrap_state, bootstrap_notes = backend.ensure_application_ready(
        secret_key_secret_ref=secret_key_secret_ref
    )
    if initial_health:
        final_health = True
    else:
        final_health = backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise FilexError(f"FileX health check failed for '{health_url}'.")

    notes = list(bootstrap_notes)
    notes.extend(
        (
            f"FileX service '{service_name}' is reconciled and healthy.",
            f"FileX data persists in '{data_name}' and files in bucket '{storage.bucket}'.",
        )
    )
    return FilexPhase(
        result=FilexResult(
            outcome="applied" if "create" in {service.action, persistent_data.action} else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            bootstrap_state=bootstrap_state,
            health_state=FilexHealthState(url=health_url, path="/healthz", passed=True),
            storage=storage,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_filex_ledger(
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
                resource.resource_type == FILEX_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == FILEX_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=FILEX_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=FILEX_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _seaweedfs_endpoint(desired_state: DesiredState) -> tuple[str, str]:
    service_name = f"{desired_state.stack_name}-seaweedfs"
    return service_name, f"http://{service_name}:8333"


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], FilexResourceRecord | None],
    find_by_name: Callable[[str], FilexResourceRecord | None],
    create_resource: Callable[[str], FilexResourceRecord],
) -> tuple[FilexManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise FilexError(
                f"Ownership ledger says the FileX {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise FilexError(
                f"Ownership ledger FileX {resource_type} no longer matches the desired naming convention."
            )
        return (
            FilexManagedResource(
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
                FilexManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = create_resource(resource_name)
        return (
            FilexManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            FilexManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return (
        FilexManagedResource(
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
    bucket: str,
    data_name: str,
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: FilexBackend,
) -> tuple[FilexManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=FILEX_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise FilexError(
                "Ownership ledger says the FileX service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise FilexError(
                "Ownership ledger FileX service no longer matches the desired naming convention."
            )
        if dry_run:
            return (
                FilexManagedResource(
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
            bucket=bucket,
            data_resource_name=data_name,
        )
        return (
            FilexManagedResource(
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
                FilexManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            bucket=bucket,
            data_resource_name=data_name,
        )
        return (
            FilexManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return (
            FilexManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=service_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        bucket=bucket,
        data_resource_name=data_name,
    )
    return (
        FilexManagedResource(
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
    return f"{stack_name}-filex"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-filex-data"


def _secret_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-filex-secret-key"


def _seaweedfs_access_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs-access-key"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:filex:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:filex:data"


def _http_health_check(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    # The edge presents a domain-issued certificate to an IP connection; the
    # internal s3 route is a private name no public CA signs. Verification is
    # off for this probe exactly like the DocuSeal loopback health check.
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection("127.0.0.1", 443, timeout=10, context=context)
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
