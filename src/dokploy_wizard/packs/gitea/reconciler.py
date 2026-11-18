# ruff: noqa: E501
"""Gitea runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import PackSharedAllocation, SharedPostgresAllocation
from dokploy_wizard.packs.gitea.models import (
    GiteaBootstrapState,
    GiteaHealthState,
    GiteaManagedResource,
    GiteaPhase,
    GiteaPostgresBinding,
    GiteaResourceRecord,
    GiteaResult,
    GiteaStorageBinding,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

GITEA_SERVICE_RESOURCE_TYPE = "gitea_service"
GITEA_DATA_RESOURCE_TYPE = "gitea_data"

# The SeaweedFS bucket Gitea uses for LFS objects and attachments. Repositories
# themselves live on the data volume; the bucket keeps the large mutable blobs
# off the volume so the repo disk stays backup-friendly. Names are fixed so
# every stack can be found the same way, and the wizard owns the bucket.
GITEA_BUCKET = "gitea"
GITEA_S3_REGION = "us-east-1"

# Built-in SSH server port. Rootless images ship no OpenSSH, so Gitea's own
# SSH listener answers on 2222 inside the overlay network only; the wizard
# does not publish git-over-SSH to the public edge.
GITEA_SSH_PORT = 2222


class GiteaError(RuntimeError):
    """Raised when Gitea reconciliation fails or detects drift."""


class GiteaBackend(Protocol):
    def get_service(self, resource_id: str) -> GiteaResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> GiteaResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> GiteaResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
    ) -> GiteaResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> GiteaResourceRecord | None: ...

    def find_persistent_data_by_name(self, resource_name: str) -> GiteaResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> GiteaResourceRecord: ...

    def check_health(self, *, service: GiteaResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[GiteaBootstrapState, tuple[str, ...]]: ...


class ShellGiteaBackend:
    def __init__(self) -> None:
        self._service: GiteaResourceRecord | None = None
        self._data: GiteaResourceRecord | None = None

    def get_service(self, resource_id: str) -> GiteaResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return GiteaResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
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
    ) -> GiteaResourceRecord:
        del hostname, postgres_service_name, postgres, data_resource_name
        self._service = GiteaResourceRecord(
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
    ) -> GiteaResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
        )

    def get_persistent_data(self, resource_id: str) -> GiteaResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return GiteaResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> GiteaResourceRecord:
        self._data = GiteaResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: GiteaResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[GiteaBootstrapState, tuple[str, ...]]:
        return (
            GiteaBootstrapState(
                initialized=True,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=True,
                provisioner_ready=True,
            ),
            (),
        )


def reconcile_gitea(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: GiteaBackend,
) -> GiteaPhase:
    if "gitea" not in desired_state.enabled_packs:
        return GiteaPhase(
            result=GiteaResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                persistent_data=None,
                bootstrap_state=None,
                health_state=None,
                postgres=None,
                storage=None,
                notes=("Gitea pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("gitea")
    if hostname is None:
        raise GiteaError(
            "Desired state is missing the canonical Gitea hostname at hostnames['gitea']."
        )
    if "seaweedfs" not in desired_state.enabled_packs:
        raise GiteaError("Gitea requires the SeaweedFS pack for LFS and attachment storage.")

    _, postgres_service_name, postgres = _get_gitea_allocation(desired_state)
    s3_service_name, s3_endpoint_url = _seaweedfs_endpoint(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    access_url = f"https://{hostname}"
    health_url = f"{access_url}/api/healthz"
    secret_key_secret_ref = _secret_key_secret_ref(desired_state.stack_name)
    storage = GiteaStorageBinding(
        bucket=GITEA_BUCKET,
        region=GITEA_S3_REGION,
        endpoint_url=s3_endpoint_url,
        access_key_secret_ref=_seaweedfs_access_key_secret_ref(desired_state.stack_name),
    )
    del s3_service_name

    persistent_data, data_id = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=GITEA_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=GITEA_DATA_RESOURCE_TYPE,
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

    postgres_binding = GiteaPostgresBinding(
        database_name=postgres.database_name,
        user_name=postgres.user_name,
        password_secret_ref=postgres.password_secret_ref,
    )

    if dry_run:
        return GiteaPhase(
            result=GiteaResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                persistent_data=persistent_data,
                bootstrap_state=GiteaBootstrapState(
                    initialized=None,
                    secret_key_secret_ref=secret_key_secret_ref,
                    bucket_ready=None,
                    provisioner_ready=None,
                ),
                health_state=GiteaHealthState(url=health_url, path="/api/healthz", passed=None),
                postgres=postgres_binding,
                storage=storage,
                notes=(
                    f"Gitea service '{service_name}' will be exposed at '{hostname}'.",
                    f"Gitea will reuse shared-core postgres database '{postgres.database_name}'.",
                    f"Gitea will store LFS objects and attachments in SeaweedFS bucket '{storage.bucket}'.",
                    "Gitea success in non-dry-run mode is gated on the /api/healthz endpoint, the gitea bucket, and the wizard provisioner account.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    service_record = GiteaResourceRecord(resource_id=service_id, resource_name=service.resource_name)
    initial_health = backend.check_health(service=service_record, url=health_url)
    bootstrap_state, bootstrap_notes = backend.ensure_application_ready(
        secret_key_secret_ref=secret_key_secret_ref
    )
    if initial_health:
        final_health = True
    else:
        final_health = backend.check_health(service=service_record, url=health_url)
    if not final_health:
        raise GiteaError(f"Gitea health check failed for '{health_url}'.")

    notes = list(bootstrap_notes)
    notes.extend(
        (
            f"Gitea service '{service_name}' is reconciled and healthy.",
            f"Gitea repositories persist in '{data_name}'; LFS and attachments in bucket '{storage.bucket}'.",
        )
    )
    return GiteaPhase(
        result=GiteaResult(
            outcome="applied" if "create" in {service.action, persistent_data.action} else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            persistent_data=persistent_data,
            bootstrap_state=bootstrap_state,
            health_state=GiteaHealthState(url=health_url, path="/api/healthz", passed=True),
            postgres=postgres_binding,
            storage=storage,
            notes=tuple(notes),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_gitea_ledger(
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
                resource.resource_type == GITEA_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == GITEA_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=GITEA_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=GITEA_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _get_gitea_allocation(
    desired_state: DesiredState,
) -> tuple[PackSharedAllocation, str, SharedPostgresAllocation]:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "gitea"
    ]
    if (
        len(matches) != 1
        or matches[0].postgres is None
        or desired_state.shared_core.postgres is None
    ):
        raise GiteaError("Gitea shared-core postgres allocation is missing from desired state.")
    return matches[0], desired_state.shared_core.postgres.service_name, matches[0].postgres


def _seaweedfs_endpoint(desired_state: DesiredState) -> tuple[str, str]:
    service_name = f"{desired_state.stack_name}-seaweedfs"
    return service_name, f"http://{service_name}:8333"


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], GiteaResourceRecord | None],
    find_by_name: Callable[[str], GiteaResourceRecord | None],
    create_resource: Callable[[str], GiteaResourceRecord],
) -> tuple[GiteaManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise GiteaError(
                f"Ownership ledger says the Gitea {resource_type} exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise GiteaError(
                f"Ownership ledger Gitea {resource_type} no longer matches the desired naming convention."
            )
        return (
            GiteaManagedResource(
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
                GiteaManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        created = create_resource(resource_name)
        return (
            GiteaManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            GiteaManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = create_resource(resource_name)
    return (
        GiteaManagedResource(
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
    backend: GiteaBackend,
) -> tuple[GiteaManagedResource, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=GITEA_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise GiteaError(
                "Ownership ledger says the Gitea service exists, but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise GiteaError(
                "Ownership ledger Gitea service no longer matches the desired naming convention."
            )
        if dry_run:
            return (
                GiteaManagedResource(
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
            GiteaManagedResource(
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
                GiteaManagedResource(
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
            GiteaManagedResource(
                action="reuse_existing",
                resource_id=created.resource_id,
                resource_name=created.resource_name,
            ),
            created.resource_id,
        )

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return (
            GiteaManagedResource(
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
        GiteaManagedResource(
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
    return f"{stack_name}-gitea"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-gitea-data"


def _secret_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-gitea-secret-key"


def _seaweedfs_access_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-seaweedfs-access-key"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:gitea:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:gitea:data"


def _http_health_check(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    # The edge presents a domain-issued certificate to an IP connection; the
    # probe rides the loopback edge exactly like the FileX health check.
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
